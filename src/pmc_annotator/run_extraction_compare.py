"""One-command orchestrator: new PMC XML -> TogoID extraction vs EuropePMC comparison.

Chains: phase1_ner plan -> {phase_regex_togoid run} || {epmc_annotations fetch ->
epmc_annotations normalize} -> compare_extractions compare -> (optional)
build_comparison_workbook.

TogoID extraction and the Europe PMC fetch+normalize chain don't depend on each
other -- they only both feed into compare -- so they run concurrently in two
threads, each driving its own subprocess chain. Their output is logged to files
under <workdir>/logs/ instead of interleaving on the console; the console just
shows start/finish. Each step's output lives under --workdir in a fixed layout,
so re-running is cheap (phase1_ner/phase_regex_togoid skip work already marked
.done; epmc fetch skips cached articles).

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


def extraction_branch(py, plan_path, regex_togoid_dir, patterns, workers, include_tables, log_path):
    cmd = [py, '-m', 'pmc_annotator.phase_regex_togoid', 'run',
           '--plan', str(plan_path), '--output', str(regex_togoid_dir),
           '--patterns', patterns, '--workers', str(workers)]
    if include_tables:
        cmd.append('--include-tables-text')
    run(cmd, 'TogoID extraction', log_path)


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
    t_all = time.time()

    run([PY, '-m', 'pmc_annotator.phase1_ner', 'plan',
         '--input', args.input, '--output', str(wd / 'phase1'),
         '--shard-size', str(args.shard_size)],
        'Plan: build shard_plan.json')

    if args.no_parallel:
        extraction_branch(PY, plan_path, regex_togoid_dir, args.patterns,
                          args.extract_workers, include_tables, None)
        epmc_branch(PY, plan_path, epmc_raw, epmc_normalized, args.mapping, args.patterns,
                   args.epmc_workers, include_tables, args.include_med, args.skip_epmc_fetch, None)
    else:
        print(f'\nRunning extraction and Europe PMC concurrently; '
              f'output logged to {log_dir}/*.log\n', flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(extraction_branch, PY, plan_path, regex_togoid_dir, args.patterns,
                           args.extract_workers, include_tables, log_dir / 'extraction.log'),
                pool.submit(epmc_branch, PY, plan_path, epmc_raw, epmc_normalized,
                           args.mapping, args.patterns, args.epmc_workers, include_tables,
                           args.include_med, args.skip_epmc_fetch, log_dir / 'epmc.log'),
            ]
            errors = []
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise SystemExit('\n'.join(errors))

    compare_cmd = [PY, '-m', 'pmc_annotator.compare_extractions', 'compare',
                   '--togoid', str(regex_togoid_dir), '--epmc', str(epmc_normalized),
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
    print(f'comparison.tsv / by_database.tsv / summary.json -> {comparison_dir}/')
    if args.xlsx:
        print(f'workbook -> {wd / "comparison.xlsx"}')


if __name__ == '__main__':
    main()
