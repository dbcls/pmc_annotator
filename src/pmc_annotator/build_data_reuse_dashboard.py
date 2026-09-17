"""Build a self-contained HTML "Data Reuse Dashboard" from build_metrics.py's
usage_long.tsv, following the recommended choices in the design-decision deck
(Framing 1-3, Decisions 1-9): confirmed-only denominator, credit-class x
data-type as a headline cross-tab, a curie-normalized "most reused" list,
dynamic AND-across/OR-within facets computed client-side (no backend), a
public/internal-QA view toggle, and Identifiers.org/Europe PMC link-outs.

Role (generated/used/mentioned) is NOT included: it requires the LLM
role-classification stage (build_windows -> role_prepass ->
build_llm_payloads -> role_llm_run), which has not been run on this
dataset and needs a live LLM server. The page says so explicitly rather
than fabricating role numbers.

  python -m pmc_annotator.build_data_reuse_dashboard \\
      --usage-long ./workdir/metrics/usage_long.tsv \\
      --patterns src/pmc_annotator/data/togoid_extract_patterns.yaml \\
      --output ./workdir/data_reuse_dashboard.html --run-label "run_17th_September"
"""
import argparse
import json
import re
from pathlib import Path

import pandas as pd
import yaml

TEMPLATE_PATH = Path(__file__).parent / 'data' / 'data_reuse_dashboard_template.html'
DEFAULT_PATTERNS = Path(__file__).parent / 'data' / 'togoid_extract_patterns.yaml'

_VER_RE = re.compile(r'\.\d+$')


def _strip_version(accession_id):
    return _VER_RE.sub('', str(accession_id).strip())


def _load_db_meta(patterns_path):
    """db -> {category (data type), uri (identifiers.org prefix)}, from the
    same yaml the extractor itself reads -- not a new/invented taxonomy."""
    spec = yaml.safe_load(Path(patterns_path).read_text())
    meta = {}
    for tier in ('safe', 'context_required', 'excluded'):
        for db, s in (spec.get(tier) or {}).items():
            meta[db] = {'data_type': s.get('category') or '(uncategorized)',
                       'uri': s.get('uri') or ''}
    return meta


def build_payload(usage_long_path, patterns_path=None, run_label=None):
    df = pd.read_csv(usage_long_path, sep='\t', dtype=str)
    df['n_in_doc'] = df['n_in_doc'].astype(int)

    db_meta = _load_db_meta(patterns_path or DEFAULT_PATTERNS)
    df['data_type'] = df['dataset'].map(lambda d: db_meta.get(d, {}).get('data_type', '(uncategorized)'))
    df['uri'] = df['dataset'].map(lambda d: db_meta.get(d, {}).get('uri', ''))
    df['norm_id'] = df['id'].map(_strip_version)
    df['curie'] = df['dataset'] + ':' + df['norm_id']

    n_links = len(df)
    n_papers = df['doc_id'].nunique()
    n_accessions = df['curie'].nunique()

    # Credit class x data type cross-tab (distinct papers per cell) --
    # the Framing-3-B headline. Rows = data type, cols = credit class.
    class_order = ['deposited_research_record', 'deposited_entity_registry',
                   'curated_entity_registry', 'derived_curated', 'out_of_scope']
    cross = (df.groupby(['data_type', 'class'])['doc_id'].nunique()
             .rename('n_papers').reset_index())
    cross_pivot = {}
    for dt, grp in cross.groupby('data_type'):
        cross_pivot[dt] = {row['class']: int(row['n_papers']) for _, row in grp.iterrows()}

    # Most-reused datasets: rank by distinct papers on the curie-normalized
    # key (Decision 3-B) so version splits (NM_x.5 / NM_x.6) don't fragment
    # the ranking.
    reused = (df.groupby(['curie', 'dataset', 'norm_id', 'uri'])['doc_id']
              .nunique().rename('n_papers').reset_index()
              .sort_values('n_papers', ascending=False).head(30))

    # Credit-class summary (KPI strip + facet option list)
    by_class = (df.groupby('class').agg(
        n_papers=('doc_id', 'nunique'), n_accessions=('curie', 'nunique'),
        n_links=('curie', 'size')).reset_index())

    by_data_type = (df.groupby('data_type').agg(
        n_papers=('doc_id', 'nunique'), n_accessions=('curie', 'nunique'),
        n_links=('curie', 'size')).reset_index().sort_values('n_links', ascending=False))

    by_db = (df.groupby('dataset').agg(
        n_papers=('doc_id', 'nunique'), n_accessions=('curie', 'nunique'),
        n_links=('curie', 'size')).reset_index().sort_values('n_links', ascending=False))

    by_oracle = (df.groupby('oracle').size().rename('n_links').reset_index()
                 .sort_values('n_links', ascending=False))
    by_confidence = (df.groupby('confidence').size().rename('n_links').reset_index()
                     .sort_values('n_links', ascending=False))

    # rows drops the per-db 'uri' (repeated ~20k times) in favor of a small
    # db -> uri lookup the page joins client-side -- meaningfully smaller
    # payload for the same data.
    rows = df[['doc_id', 'dataset', 'norm_id', 'curie', 'class', 'data_type',
              'oracle', 'confidence', 'n_in_doc']].rename(
        columns={'dataset': 'db', 'norm_id': 'id'})
    db_uri = {db: m['uri'] for db, m in db_meta.items() if m.get('uri')}

    return {
        'run_label': run_label or '',
        'n_links': n_links,
        'n_papers': n_papers,
        'n_accessions': n_accessions,
        'class_order': class_order,
        'cross_pivot': cross_pivot,
        'most_reused': reused.drop(columns=['uri']).to_dict(orient='records'),
        'by_class': by_class.to_dict(orient='records'),
        'by_data_type': by_data_type.to_dict(orient='records'),
        'by_db': by_db.to_dict(orient='records'),
        'by_oracle': by_oracle.to_dict(orient='records'),
        'by_confidence': by_confidence.to_dict(orient='records'),
        'db_uri': db_uri,
        'rows': rows.to_dict(orient='records'),
    }


def build(usage_long_path, output, patterns_path=None, run_label=None):
    payload = build_payload(usage_long_path, patterns_path, run_label)
    template = TEMPLATE_PATH.read_text(encoding='utf-8')
    html = template.replace('__DATA_PLACEHOLDER__', json.dumps(payload))
    Path(output).write_text(html, encoding='utf-8')
    print(f'Wrote {output} ({payload["n_papers"]:,} papers, {payload["n_accessions"]:,} '
          f'distinct accessions, {payload["n_links"]:,} links)')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--usage-long', required=True, help='build_metrics.py usage_long.tsv')
    ap.add_argument('--patterns', default=str(DEFAULT_PATTERNS),
                    help='togoid_extract_patterns.yaml (source of the data-type/uri map)')
    ap.add_argument('--output', required=True, help='.html path to write')
    ap.add_argument('--run-label', default=None)
    args = ap.parse_args()
    build(args.usage_long, args.output, args.patterns, args.run_label)


if __name__ == '__main__':
    main()
