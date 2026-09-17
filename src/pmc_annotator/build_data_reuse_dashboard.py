"""Build a self-contained HTML "Data Reuse Dashboard" from build_metrics.py's
usage_long.tsv, following the recommended choices in the design-decision deck
(Framing 1-3, Decisions 1-9): a denominator that excludes out_of_scope
(Decision 1), a normalized links-per-paper rate alongside absolute counts
(Decision 2, partial -- no year-based rate without an oa_file_list.csv),
credit-class x data-type as a headline cross-tab (Framing 3), a
curie-normalized "most reused" list with version suffixes collapsed
(Decision 3), dynamic AND-across/OR-within facets computed client-side, no
backend (Decision 4/5), a data-provenance line (Decision 9), an evidence-span
detail view per row plus an occurrence count when --windows is given
(Decision 6 / Framing 1), Identifiers.org/Europe PMC link-outs (Decision 7),
and a public/internal-QA page-tab pair (Decision 8).

Role (generated/used/mentioned) is NOT included: it requires the LLM
role-classification stage (build_windows -> role_prepass ->
build_llm_payloads -> role_llm_run), which has not been run on this
dataset and needs a live LLM server. The page says so explicitly rather
than fabricating role numbers.

  python -m pmc_annotator.build_data_reuse_dashboard \\
      --usage-long ./workdir/metrics/usage_long.tsv \\
      --patterns src/pmc_annotator/data/togoid_extract_patterns.yaml \\
      --windows ./workdir/windows.jsonl \\
      --output ./workdir/data_reuse_dashboard.html --run-label "run_17th_September"

--windows expects build_windows.py run with --per-mention (no LLM needed):
  python -m pmc_annotator.build_windows \\
      --usage ./workdir/metrics/usage_long.tsv \\
      --parquet-dir ./workdir/regex_togoid/accession_annotations_togoid \\
      --xml-root /path/to/PMC_Data --out ./workdir/windows.jsonl --per-mention
  # --xml-root is the PARENT of the PMC0xxxxxxxxx-style shard folders (it
  # builds "PMC{id//1_000_000:03d}xxxxxx" itself) -- not that folder directly.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

TEMPLATE_PATH = Path(__file__).parent / 'data' / 'data_reuse_dashboard_template.html'
DEFAULT_PATTERNS = Path(__file__).parent / 'data' / 'togoid_extract_patterns.yaml'

_VER_RE = re.compile(r'\.\d+$')


def _strip_version(accession_id):
    return _VER_RE.sub('', str(accession_id).strip())


def _truncate_around_marker(text, radius=220):
    """window_text already carries the target accession marked as
    «...» (build_windows.py's _mark()). Truncates further, centered on
    that marker, so the embedded payload doesn't have to carry the full
    (sometimes 1500-char, sometimes a whole flattened table row) window
    for all ~19k links -- only enough to read in a detail card."""
    i = text.find('«')  # '«'
    if i == -1:
        return text[:radius * 2] + ('...' if len(text) > radius * 2 else '')
    lo, hi = max(0, i - radius), min(len(text), i + radius)
    prefix = '...' if lo > 0 else ''
    suffix = '...' if hi < len(text) else ''
    return prefix + text[lo:hi] + suffix


def _load_evidence(windows_path):
    """(doc_id, dataset, id) -> {section_type, window_text, n_occurrences,
    align_ok}, one representative record per key (mirrors build_windows.py's
    own non-per-mention selection: gen-cue > use-cue > first non-flat-table
    > first) plus a count of how many mentions were seen in this paper
    (Framing 1's "occurrence is a drill-down detail under the pair-row").
    windows.jsonl itself is per-mention and can be tens of MB; this keeps
    only what the page needs."""
    # Keyed on the version-stripped id, same as everywhere else in this page
    # (rows/curie use norm_id) -- usage_long.tsv's raw id can carry a
    # version suffix (e.g. AC019220.3), which build_windows.py passes
    # through unchanged, so this also merges occurrences of different
    # versions of the same accession into one evidence entry.
    groups = {}
    with open(windows_path, encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            key = (r['doc_id'], r['dataset'], _strip_version(r['id']))
            groups.setdefault(key, []).append(r)

    evidence = {}
    for key, recs in groups.items():
        matched = [r for r in recs if r.get('matched')]
        if not matched:
            continue
        ranked = [r for r in matched if not r.get('flat_table')] or matched
        best = (next((r for r in ranked if r.get('gen_cue')), None)
                or next((r for r in ranked if r.get('use_cue')), None)
                or ranked[0])
        doc_id, dataset, ent_id = key
        evidence[f'{doc_id}||{dataset}||{ent_id}'] = {
            'section_type': best.get('section_type') or '',
            'window_text': _truncate_around_marker(best.get('window_text', '')),
            'n_occurrences': len(matched),
            'align_ok': bool(best.get('align_ok')),
        }
    return evidence


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


def build_payload(usage_long_path, patterns_path=None, run_label=None, windows_path=None):
    df = pd.read_csv(usage_long_path, sep='\t', dtype=str)
    df['n_in_doc'] = df['n_in_doc'].astype(int)

    db_meta = _load_db_meta(patterns_path or DEFAULT_PATTERNS)
    df['data_type'] = df['dataset'].map(lambda d: db_meta.get(d, {}).get('data_type', '(uncategorized)'))
    df['uri'] = df['dataset'].map(lambda d: db_meta.get(d, {}).get('uri', ''))
    df['norm_id'] = df['id'].map(_strip_version)
    df['curie'] = df['dataset'] + ':' + df['norm_id']

    # Decision 1's exact denominator: "status = confirmed AND class !=
    # out_of_scope". usage_long.tsv is already confirmed-only by
    # construction (build_metrics.py); out_of_scope rows still need
    # excluding here explicitly. out_of_scope stays visible elsewhere
    # (its own column in the cross-tab, its own facet option) -- it's
    # excluded from the *denominator*, not hidden from the page.
    denom = df[df['class'] != 'out_of_scope']
    n_links = len(denom)
    n_papers = denom['doc_id'].nunique()
    n_accessions = denom['curie'].nunique()
    links_per_paper = round(n_links / n_papers, 1) if n_papers else 0

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

    n_out_of_scope_excluded = len(df) - n_links
    evidence = _load_evidence(windows_path) if windows_path and Path(windows_path).exists() else {}

    return {
        'run_label': run_label or '',
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'source_file': Path(usage_long_path).name,
        'n_links': n_links,
        'n_papers': n_papers,
        'n_accessions': n_accessions,
        'links_per_paper': links_per_paper,
        'n_out_of_scope_excluded': n_out_of_scope_excluded,
        'class_order': class_order,
        'cross_pivot': cross_pivot,
        'most_reused': reused.drop(columns=['uri']).to_dict(orient='records'),
        'by_class': by_class.to_dict(orient='records'),
        'by_data_type': by_data_type.to_dict(orient='records'),
        'by_db': by_db.to_dict(orient='records'),
        'by_oracle': by_oracle.to_dict(orient='records'),
        'by_confidence': by_confidence.to_dict(orient='records'),
        'db_uri': db_uri,
        'evidence': evidence,
        'rows': rows.to_dict(orient='records'),
    }


def build(usage_long_path, output, patterns_path=None, run_label=None, windows_path=None):
    payload = build_payload(usage_long_path, patterns_path, run_label, windows_path)
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
    ap.add_argument('--windows', default=None,
                    help='Optional windows.jsonl from build_windows.py (--per-mention). Adds an '
                         'evidence-span detail view per row (Decision 6) and an occurrence count '
                         '(Framing 1) -- both skipped if not given.')
    args = ap.parse_args()
    build(args.usage_long, args.output, args.patterns, args.run_label, args.windows)


if __name__ == '__main__':
    main()
