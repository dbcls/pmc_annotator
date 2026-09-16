"""One-command orchestrator: new PMC XML -> TogoID extraction vs EuropePMC comparison.

Chains, in order: phase1_ner plan -> phase_regex_togoid run -> epmc_annotations
fetch -> epmc_annotations normalize -> compare_extractions compare -> (optional)
build_comparison_workbook. Each step's output already lives under --workdir in
a fixed layout, so re-running is cheap (phase1_ner/phase_regex_togoid skip work
that's already marked .done; epmc fetch skips cached articles).

  python -m pmc_annotator.run_extraction_compare \\
      --input /path/to/new_pmc_xml --workdir ./workdir --xlsx
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
DEFAULT_RECONCILIATION = _REPO_ROOT / 'outputs' / 'namespace_reconciliation.tsv'
DEFAULT_MAPPING = Path(__file__).parent / 'data' / 'epmc_mapping.yaml'
DEFAULT_PATTERNS = Path(__file__).parent / 'data' / 'togoid_extract_patterns.yaml'


def run(cmd, label):
    print(f'\n=== {label} ===')
    print('  $ ' + ' '.join(str(c) for c in cmd), flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(f'[{label}] failed (rc={proc.returncode})')
    print(f'  ({time.time() - t0:.0f}s)')


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
    ap.add_argument('--xlsx', action='store_true', help='Also build a formatted .xlsx workbook')
    args = ap.parse_args()

    PY = sys.executable
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)
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

    extract_cmd = [PY, '-m', 'pmc_annotator.phase_regex_togoid', 'run',
                   '--plan', str(plan_path), '--output', str(regex_togoid_dir),
                   '--patterns', args.patterns, '--workers', str(args.extract_workers)]
    if include_tables:
        extract_cmd.append('--include-tables-text')
    run(extract_cmd, 'TogoID extraction (phase_regex_togoid)')

    if not args.skip_epmc_fetch:
        fetch_cmd = [PY, '-m', 'pmc_annotator.epmc_annotations', 'fetch',
                     '--plan', str(plan_path), '--output', str(epmc_raw),
                     '--workers', str(args.epmc_workers)]
        if args.include_med:
            fetch_cmd.append('--include-med')
        run(fetch_cmd, 'Europe PMC fetch')
    else:
        print('\n=== Europe PMC fetch === skipped (--skip-epmc-fetch), reusing cache')

    normalize_cmd = [PY, '-m', 'pmc_annotator.epmc_annotations', 'normalize',
                     '--plan', str(plan_path), '--raw', str(epmc_raw),
                     '--mapping', args.mapping, '--patterns', args.patterns,
                     '--output', str(epmc_normalized)]
    if include_tables:
        normalize_cmd.append('--include-tables-text')
    run(normalize_cmd, 'Europe PMC normalize')

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
