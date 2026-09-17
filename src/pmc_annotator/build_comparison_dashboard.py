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


def build_payload(comparison_dir, run_label=None, rejected_path=None):
    comparison_dir = Path(comparison_dir)
    doc = pd.read_csv(comparison_dir / 'by_document.tsv', sep='\t')
    hist = pd.read_csv(comparison_dir / 'document_agreement_histogram.tsv', sep='\t')
    dbdf = pd.read_csv(comparison_dir / 'by_database.tsv', sep='\t')
    comparison = pd.read_csv(comparison_dir / 'comparison.tsv', sep='\t',
                             usecols=['doc_id', 'db', 'membership'])
    summary = json.loads((comparison_dir / 'summary.json').read_text())

    for col in ('both', 'togoid_only', 'europepmc_only'):
        if col not in dbdf:
            dbdf[col] = 0
    dbdf = dbdf.copy()
    dbdf['total'] = dbdf['both'] + dbdf['togoid_only'] + dbdf['europepmc_only']
    all_db = dbdf.sort_values('total', ascending=False)[
        ['db', 'both', 'togoid_only', 'europepmc_only', 'total']]
    top_db = all_db.head(15)

    # Databases Europe PMC finds that TogoID's extractor never proposes at
    # all -- both and togoid_only are 0, so every mention of this db is
    # europepmc_only. On its own this under-counts: a db TogoID has *zero*
    # pattern coverage for (not merely a db that happened to score 0 here)
    # is invisible to resolve_dataset() entirely, so its EPMC mentions never
    # even reach comparison.tsv -- they're rejected upstream as
    # 'unmapped_or_ambiguous_namespace' instead. rejected_by_label below
    # covers that gap; this covers the (currently empty, but real) case of
    # a db epmc_mapping.yaml *can* classify that TogoID still never matches.
    epmc_only_dbs = all_db[(all_db['both'] == 0) & (all_db['togoid_only'] == 0)
                           & (all_db['europepmc_only'] > 0)]

    # db -> every doc_id that db appears in (any membership), so a click on
    # a database can filter the per-document table to its papers, the same
    # way clicking a histogram bar filters to an agreement bucket.
    db_to_docs = (comparison.groupby('db')['doc_id']
                  .apply(lambda s: sorted(set(s))).to_dict())

    # Databases Europe PMC labels but resolve_dataset() can't classify into
    # any TogoID dataset key at all (no yaml pattern, no epmc_mapping.yaml
    # override) -- these never reach comparison.tsv/by_database.tsv, so
    # epmc_only_dbs above misses them entirely. This is the real answer to
    # "what does Europe PMC see that TogoID has zero coverage for": e.g.
    # PDBe, RefSNP (dbsnp), OMIM, ArrayExpress routinely dwarf anything in
    # epmc_only_dbs. rejected.json's 'reason' field also has
    # 'missing_or_ambiguous_local_span' (namespace WAS classified but the
    # exact text couldn't be located in the XML) -- excluded here since
    # that's a locate() bug/ambiguity, not a coverage gap.
    rejected_by_label = {}
    if rejected_path and Path(rejected_path).exists():
        rejected = json.loads(Path(rejected_path).read_text())
        unmapped = [r for r in rejected if r.get('reason') == 'unmapped_or_ambiguous_namespace']
        by_label = {}
        for r in unmapped:
            label = (r.get('annotation') or {}).get('memberOf') or '(unlabeled)'
            by_label.setdefault(label, set()).add(r['doc_id'])
        # mention counts (rows), separate from distinct-paper counts (docs)
        mention_counts = {}
        for r in unmapped:
            label = (r.get('annotation') or {}).get('memberOf') or '(unlabeled)'
            mention_counts[label] = mention_counts.get(label, 0) + 1
        rejected_by_label = {
            label: {'mentions': mention_counts[label], 'n_docs': len(docs),
                    'doc_ids': sorted(docs)}
            for label, docs in sorted(by_label.items(), key=lambda kv: -len(kv[1]))
        }

    with_data = doc[doc['has_data']] if 'has_data' in doc else doc[doc['total'] > 0]
    scatter = with_data[['doc_id', 'both', 'togoid_only', 'europepmc_only', 'total', 'pct_both']]

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
        'all_db': all_db.to_dict(orient='records'),
        'epmc_only_dbs': epmc_only_dbs.to_dict(orient='records'),
        'db_to_docs': db_to_docs,
        'rejected_by_label': rejected_by_label,
        'scatter': scatter.to_dict(orient='records'),
    }


def build(comparison_dir, output, run_label=None, rejected_path=None):
    payload = build_payload(comparison_dir, run_label, rejected_path)
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
    ap.add_argument('--rejected', default=None,
                    help='Optional rejected.json from epmc_annotations.py normalize -- adds a '
                         '"databases Europe PMC sees but TogoID has zero coverage for" section')
    args = ap.parse_args()
    build(args.comparison_dir, args.output, args.run_label, args.rejected)


if __name__ == '__main__':
    main()
