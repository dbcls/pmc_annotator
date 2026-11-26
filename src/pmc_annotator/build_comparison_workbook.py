"""Build a formatted Excel workbook from compare_extractions.py's output.

Reads comparison.tsv / by_database.tsv / summary.json from a `compare`
output directory (and optionally rejected.json from epmc_annotations.py
normalize) and writes one .xlsx with a sheet per table. Uses openpyxl only
-- no private/internal tooling, runs anywhere the repo's own deps do.
"""
import argparse
import json
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
HEADER_FONT = Font(name='Arial', bold=True, color='FFFFFF')
BODY_FONT = Font(name='Arial', size=10)


def _write_table(writer, sheet_name, df):
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = 'A2'
    for col_idx, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal='center', vertical='center')
        sample = df[col].astype(str).head(200) if len(df) else []
        width = max([len(str(col))] + [len(v) for v in sample])
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width + 2, 60)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            cell.font = BODY_FONT


def _extract_exact(annotation):
    target = annotation.get('target')
    body = annotation.get('body')
    if isinstance(target, str) and isinstance(body, dict):
        return body.get('value', '')
    selector = target.get('selector', {}) if isinstance(target, dict) else {}
    return selector.get('exact', '')


def flatten_rejected(rejected):
    rows = []
    for r in rejected:
        ann = r.get('annotation', {})
        rows.append({
            'doc_id': r.get('doc_id'),
            'reason': r.get('reason'),
            'memberOf': ann.get('memberOf', ''),
            'exact': _extract_exact(ann),
            'n_local_candidates': len(r['local_candidates']) if 'local_candidates' in r else None,
        })
    return pd.DataFrame(rows)


def build(comparison_dir, output, rejected_path=None):
    comparison_dir = Path(comparison_dir)
    comparison = pd.read_csv(comparison_dir / 'comparison.tsv', sep='\t')
    by_database = pd.read_csv(comparison_dir / 'by_database.tsv', sep='\t')
    summary = json.loads((comparison_dir / 'summary.json').read_text())
    by_document_path = comparison_dir / 'by_document.tsv'
    histogram_path = comparison_dir / 'document_agreement_histogram.tsv'

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        summary_df = pd.DataFrame(list(summary.items()), columns=['metric', 'value'])
        _write_table(writer, 'Summary', summary_df)
        _write_table(writer, 'Comparison', comparison)
        _write_table(writer, 'By Database', by_database)
        if by_document_path.exists():
            _write_table(writer, 'By Document', pd.read_csv(by_document_path, sep='\t'))
        if histogram_path.exists():
            _write_table(writer, 'Agreement Histogram', pd.read_csv(histogram_path, sep='\t'))
        if rejected_path:
            rejected = json.loads(Path(rejected_path).read_text())
            _write_table(writer, 'Rejected', flatten_rejected(rejected))

    print(f'Wrote {output}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--comparison-dir', required=True,
                     help="compare_extractions.py compare's --output dir "
                          '(comparison.tsv, by_database.tsv, summary.json)')
    ap.add_argument('--rejected', default=None,
                     help='Optional rejected.json from epmc_annotations.py normalize')
    ap.add_argument('--output', required=True, help='.xlsx path to write')
    args = ap.parse_args()
    build(args.comparison_dir, args.output, args.rejected)


if __name__ == '__main__':
    main()
