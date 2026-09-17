"""Build a self-contained HTML dashboard from compare_extractions.py's output.

Reads by_document.tsv, document_agreement_histogram.tsv, by_database.tsv and
summary.json from a `compare` output directory and writes one static .html:
KPI tiles, the per-document agreement histogram, a top-databases grouped bar
chart, a per-document divergence scatter, and a searchable/sortable table.
No server-side dependency at view time -- the page embeds its own data as a
JSON literal, so it opens directly from disk or from any static file host
(a local `python -m http.server`, or a GitHub Pages `docs/` folder).

  python -m pmc_annotator.build_comparison_dashboard \\
      --comparison-dir ./workdir/comparison --output ./workdir/dashboard.html \\
      --run-label "run_17th_September"
"""
import argparse
import json
from pathlib import Path

import pandas as pd

TEMPLATE_PATH = Path(__file__).parent / 'data' / 'comparison_dashboard_template.html'


def build_payload(comparison_dir, run_label=None):
    comparison_dir = Path(comparison_dir)
    doc = pd.read_csv(comparison_dir / 'by_document.tsv', sep='\t')
    hist = pd.read_csv(comparison_dir / 'document_agreement_histogram.tsv', sep='\t')
    dbdf = pd.read_csv(comparison_dir / 'by_database.tsv', sep='\t')
    summary = json.loads((comparison_dir / 'summary.json').read_text())

    for col in ('both', 'togoid_only', 'europepmc_only'):
        if col not in dbdf:
            dbdf[col] = 0
    dbdf = dbdf.copy()
    dbdf['total'] = dbdf['both'] + dbdf['togoid_only'] + dbdf['europepmc_only']
    top_db = dbdf.sort_values('total', ascending=False).head(15)[
        ['db', 'both', 'togoid_only', 'europepmc_only', 'total']]

    with_data = doc[doc['has_data']] if 'has_data' in doc else doc[doc['total'] > 0]
    scatter = with_data[['doc_id', 'both', 'togoid_only', 'europepmc_only', 'total']]

    n_total = len(doc)
    n_with_data = int(with_data.shape[0])

    return {
        'run_label': run_label or '',
        'summary': summary,
        'n_total_papers': n_total,
        'n_with_data': n_with_data,
        'n_no_data': n_total - n_with_data,
        'histogram': hist.to_dict(orient='records'),
        'top_db': top_db.to_dict(orient='records'),
        'scatter': scatter.to_dict(orient='records'),
    }


def build(comparison_dir, output, run_label=None):
    payload = build_payload(comparison_dir, run_label)
    template = TEMPLATE_PATH.read_text(encoding='utf-8')
    html = template.replace('__DATA_PLACEHOLDER__', json.dumps(payload))
    Path(output).write_text(html, encoding='utf-8')
    print(f'Wrote {output} ({payload["n_with_data"]:,}/{payload["n_total_papers"]:,} '
          f'papers with >=1 accession)')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--comparison-dir', required=True,
                    help="compare_extractions.py compare's --output dir "
                         '(by_document.tsv, document_agreement_histogram.tsv, '
                         'by_database.tsv, summary.json)')
    ap.add_argument('--output', required=True, help='.html path to write')
    ap.add_argument('--run-label', default=None,
                    help='Optional label shown in the page subtitle (e.g. a run/date name)')
    args = ap.parse_args()
    build(args.comparison_dir, args.output, args.run_label)


if __name__ == '__main__':
    main()
