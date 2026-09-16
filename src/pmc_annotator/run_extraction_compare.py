"""One-command orchestrator: new PMC XML -> TogoID extraction vs EuropePMC comparison.

Chains:
  phase1_ner plan
    -> { phase_regex_togoid run
           -> build_mentions -> verify_t2 -> verify_t3 -> verify_idorg
           -> filter_verified_extraction   }
    || { epmc_annotations fetch -> epmc_annotations normalize }
    -> compare_extractions compare
    -> (optional) build_comparison_workbook

By default, compare runs against only the *existence-confirmed* half of our
extraction (T2 -> T3 -> verify_idorg, then filtered), not the raw syntactic
matches -- so the comparison measures agreement on data we've actually
verified exists, not on unverified extraction noise. Pass --skip-verify to
compare against the raw extraction instead (faster, but includes anything
verification would have caught, e.g. accession-shaped false positives).

TogoID extraction(+verification) and the Europe PMC fetch+normalize chain
don't depend on each other -- they only both feed into compare -- so they
run concurrently in two threads, each driving its own subprocess chain.
Their output is logged to files under <workdir>/logs/ instead of
interleaving on the console; the console just shows start/finish. Each
step's output lives under --workdir in a fixed layout, so re-running is
cheap (phase1_ner/phase_regex_togoid skip work already marked .done; epmc
fetch skips cached articles; verify_t2/t3/idorg cache their lookups).

  python -m pmc_annotator.run_extraction_compare \\
      --input /path/to/new_pmc_xml --workdir ./workdir --xlsx
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
DEFAULT_RECONCILIATION = _REPO_ROOT / 'outputs' / 'namespace_reconciliation.tsv'
DEFAULT_MAPPING = Path(__file__).parent / 'data' / 'epmc_mapping.yaml'
DEFAULT_PATTERNS = Path(__file__).parent / 'data' / 'togoid_extract_patterns.yaml'


def run(cmd, label, log_path=None):
    """Run one subprocess. With a log_path, output is written there (for steps
    running concurrently, so their output doesn't interleave on the console);
    otherwise it streams straight to the console."""
    dest = f'log: {log_path}' if log_path else 'console'
    print(f'[{label}] starting ({dest})', flush=True)
    print(f'[{label}]   $ ' + ' '.join(str(c) for c in cmd), flush=True)
    t0 = time.time()
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, 'a') as f:
            f.write(f'\n=== {label} ===\n$ {" ".join(str(c) for c in cmd)}\n')
            f.flush()
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    else:
        proc = subprocess.run(cmd)
    dt = time.time() - t0
    if proc.returncode != 0:
        raise RuntimeError(f'[{label}] failed (rc={proc.returncode})'
                           + (f' -- see {log_path}' if log_path else ''))
    print(f'[{label}] done ({dt:.0f}s)', flush=True)


def extraction_branch(py, wd, plan_path, regex_togoid_dir, patterns, workers, include_tables,
                      verify, idorg_workers, idorg_sleep, log_path):
    cmd = [py, '-m', 'pmc_annotator.phase_regex_togoid', 'run',
           '--plan', str(plan_path), '--output', str(regex_togoid_dir),
           '--patterns', patterns, '--workers', str(workers)]
    if include_tables:
        cmd.append('--include-tables-text')
    run(cmd, 'TogoID extraction', log_path)

    if not verify:
        return regex_togoid_dir

    mentions = wd / 'verify' / 'mentions.tsv'
    t2_input = wd / 'verify' / 't2_input.tsv'
    verified_t2 = wd / 'verify' / 'verified.tsv'
    verified_t3 = wd / 'verify' / 'verified.t3.tsv'
    verified_idorg = wd / 'verify' / 'verified.idorg.tsv'
    verified_dir = wd / 'regex_togoid_verified'

    run([py, '-m', 'pmc_annotator.build_mentions',
         '--togoid', str(regex_togoid_dir),
         '--mentions-out', str(mentions), '--t2-input-out', str(t2_input)],
        'Verify: build mentions', log_path)
    run([py, '-m', 'pmc_annotator.verify_t2', 'verify',
         '--in', str(t2_input), '--out', str(verified_t2)],
        'Verify: T2 (SPARQL/RDF Portal)', log_path)
    run([py, '-m', 'pmc_annotator.verify_t3', 'run',
         '--in', str(verified_t2), '--out', str(verified_t3)],
        'Verify: T3 (NCBI E-utilities)', log_path)
    run([py, '-m', 'pmc_annotator.verify_idorg', 'run',
         '--in', str(verified_t3), '--out', str(verified_idorg),
         '--workers', str(idorg_workers), '--sleep', str(idorg_sleep)],
        'Verify: identifiers.org resolution', log_path)
    run([py, '-m', 'pmc_annotator.filter_verified_extraction',
         '--togoid', str(regex_togoid_dir), '--verified', str(verified_idorg),
         '--output', str(verified_dir)],
        'Verify: filter to confirmed', log_path)
    return verified_dir


def epmc_branch(py, plan_path, epmc_raw, epmc_normalized, mapping, patterns,
                workers, include_tables, include_med, skip_fetch, log_path):
    if not skip_fetch:
        fetch_cmd = [py, '-m', 'pmc_annotator.epmc_annotations', 'fetch',
                     '--plan', str(plan_path), '--output', str(epmc_raw),
                     '--workers', str(workers)]
        if include_med:
            fetch_cmd.append('--include-med')
        run(fetch_cmd, 'Europe PMC fetch', log_path)
    else:
        print('[Europe PMC fetch] skipped (--skip-epmc-fetch), reusing cache', flush=True)

    normalize_cmd = [py, '-m', 'pmc_annotator.epmc_annotations', 'normalize',
                     '--plan', str(plan_path), '--raw', str(epmc_raw),
                     '--mapping', mapping, '--patterns', patterns,
                     '--output', str(epmc_normalized)]
    if include_tables:
        normalize_cmd.append('--include-tables-text')
    run(normalize_cmd, 'Europe PMC normalize', log_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, help='Root dir of JATS XML (new dataset)')
    ap.add_argument('--workdir', required=True, help='Output root; every stage writes under here')
    ap.add_argument('--shard-size', type=int, default=500)
    ap.add_argument('--extract-workers', type=int, default=8, help='phase_regex_togoid --workers')
    ap.add_argument('--epmc-workers', type=int, default=8, help='epmc_annotations fetch --workers')
    ap.add_argument('--patterns', default=str(DEFAULT_PATTERNS))
    ap.add_argument('--mapping', default=str(DEFAULT_MAPPING))
    ap.add_argument('--reconciliation', default=str(DEFAULT_RECONCILIATION),
                    help='namespace_reconciliation.tsv; pass "" to skip the registry-metadata join')
    ap.add_argument('--no-tables', action='store_true',
                    help='Disable --include-tables-text on extraction/normalize (on by default)')
    ap.add_argument('--include-med', action='store_true', help='Also fetch MED abstract annotations')
    ap.add_argument('--skip-epmc-fetch', action='store_true',
                    help='Reuse an existing raw.jsonl cache instead of re-fetching from Europe PMC')
    ap.add_argument('--no-parallel', action='store_true',
                    help='Run extraction and the Europe PMC chain sequentially instead of concurrently')
    ap.add_argument('--skip-verify', action='store_true',
                    help='Compare against raw extraction instead of running T2 -> T3 -> '
                         'verify_idorg and comparing only existence-confirmed accessions')
    ap.add_argument('--verify-idorg-workers', type=int, default=8)
    ap.add_argument('--verify-idorg-sleep', type=float, default=0.2)
    ap.add_argument('--xlsx', action='store_true', help='Also build a formatted .xlsx workbook')
    args = ap.parse_args()

    PY = sys.executable
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    log_dir = wd / 'logs'
    plan_path = wd / 'phase1' / 'shard_plan.json'
    regex_togoid_dir = wd / 'regex_togoid'
    epmc_raw = wd / 'epmc' / 'raw.jsonl'
    epmc_normalized = wd / 'epmc' / 'normalized'
    comparison_dir = wd / 'comparison'
    include_tables = not args.no_tables
    verify = not args.skip_verify
    t_all = time.time()

    run([PY, '-m', 'pmc_annotator.phase1_ner', 'plan',
         '--input', args.input, '--output', str(wd / 'phase1'),
         '--shard-size', str(args.shard_size)],
        'Plan: build shard_plan.json')

    if args.no_parallel:
        togoid_compare_dir = extraction_branch(
            PY, wd, plan_path, regex_togoid_dir, args.patterns, args.extract_workers,
            include_tables, verify, args.verify_idorg_workers, args.verify_idorg_sleep, None)
        epmc_branch(PY, plan_path, epmc_raw, epmc_normalized, args.mapping, args.patterns,
                   args.epmc_workers, include_tables, args.include_med, args.skip_epmc_fetch, None)
    else:
        print(f'\nRunning extraction{" + verification" if verify else ""} and Europe PMC '
              f'concurrently; output logged to {log_dir}/*.log\n', flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            extraction_future = pool.submit(
                extraction_branch, PY, wd, plan_path, regex_togoid_dir, args.patterns,
                args.extract_workers, include_tables, verify,
                args.verify_idorg_workers, args.verify_idorg_sleep, log_dir / 'extraction.log')
            epmc_future = pool.submit(
                epmc_branch, PY, plan_path, epmc_raw, epmc_normalized,
                args.mapping, args.patterns, args.epmc_workers, include_tables,
                args.include_med, args.skip_epmc_fetch, log_dir / 'epmc.log')

            errors, togoid_compare_dir = [], regex_togoid_dir
            try:
                togoid_compare_dir = extraction_future.result()
            except Exception as exc:
                errors.append(str(exc))
            try:
                epmc_future.result()
            except Exception as exc:
                errors.append(str(exc))
            if errors:
                raise SystemExit('\n'.join(errors))

    compare_cmd = [PY, '-m', 'pmc_annotator.compare_extractions', 'compare',
                   '--togoid', str(togoid_compare_dir), '--epmc', str(epmc_normalized),
                   '--output', str(comparison_dir), '--plan', str(plan_path)]
    if args.reconciliation:
        compare_cmd += ['--reconciliation', args.reconciliation]
    run(compare_cmd, 'Compare TogoID vs Europe PMC')

    if args.xlsx:
        xlsx_cmd = [PY, '-m', 'pmc_annotator.build_comparison_workbook',
                    '--comparison-dir', str(comparison_dir),
                    '--rejected', str(epmc_normalized / 'rejected.json'),
                    '--output', str(wd / 'comparison.xlsx')]
        run(xlsx_cmd, 'Build Excel workbook')

    print(f'\n=== DONE in {time.time() - t_all:.0f}s ===')
    print(f'compared against: {"existence-confirmed (T2/T3/verify_idorg)" if verify else "raw extraction (--skip-verify)"}')
    print(f'comparison.tsv / by_database.tsv / summary.json -> {comparison_dir}/')
    if args.xlsx:
        print(f'workbook -> {wd / "comparison.xlsx"}')


if __name__ == '__main__':
    main()
