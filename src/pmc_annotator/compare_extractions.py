"""Merge aligned annotation layers or compare paper/database/literal-accession sets."""
import argparse
import json
from pathlib import Path
import pandas as pd

from .preprocess import JATSParser


def read_layer(path):
    path = Path(path)
    files = [path] if path.is_file() else sorted(path.rglob('shard_*.parquet'))
    if not files:
        raise ValueError(f'No shard Parquet files in {path}')
    df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    for name in ('doc_id', 'db', 'surface', 'offset', 'source'):
        if name not in df:
            raise ValueError(f'Missing {name} in {path}')
    if (df.offset < 0).any():
        raise ValueError('Unaligned rows cannot be merged as mention occurrences')
    return df


def load_reconciliation(path):
    """Per-database metadata from reconcile_namespaces.py's TSV report.

    Joined onto the comparison output by dataset key (``db``) so every row
    also carries the identifiers.org registry context (name, canonical URI,
    deprecation, and whether the mapping is direct/split/alias/unregistered)
    without re-deriving it here.
    """
    frame = pd.read_csv(path, sep='\t', dtype=str).drop_duplicates('dataset_key')
    frame = frame.rename(columns={
        'dataset_key': 'db',
        'current_uri': 'registry_identifiers_org_uri',
    })
    return frame[['db', 'category', 'registry_prefix', 'mapping_type', 'mapping_source',
                  'registry_status', 'registry_name', 'registry_pattern',
                  'registry_deprecated', 'registry_identifiers_org_uri', 'review_note']]


def passage_previews(plan_path, include_tables_text=False):
    """Returns (previews, all_doc_ids). all_doc_ids is every doc_id in the plan
    (not just ones with a hit on either side) -- used by document_level() so
    papers with zero accessions on both sides still get a 0/0/0 row instead
    of silently disappearing from by_document.tsv.

    include_tables_text MUST match whatever the extraction/normalize steps
    used (phase_regex_togoid.py / epmc_annotations.py --include-tables-text,
    on by default in run_extraction_compare.py). Passage indices are only
    comparable when the same JATSParser settings produced them -- with it
    left at the JATSParser default (off) while extraction ran with tables
    included, every table-sourced match silently gets no preview at all
    (its passage_idx doesn't exist in this doc's non-table passage list),
    even though the match itself is correct."""
    if not plan_path:
        return {}, set()
    plan = json.loads(Path(plan_path).read_text())
    parser = JATSParser(include_captions=True, include_tables_text=include_tables_text)
    previews, all_doc_ids = {}, set()
    for shard in plan['shards']:
        for filename in shard['files']:
            doc = parser.parse(Path(filename))
            if doc is not None:
                all_doc_ids.add(doc.id)
                for index, passage in enumerate(doc.passages):
                    previews[(doc.id, index)] = passage.text[:300]
    return previews, all_doc_ids


def occurrence_details(df, prefix, previews):
    """One representative occurrence plus all local locations for a key."""
    keys = ['doc_id', 'db', 'surface']
    frame = df.copy()
    frame['passage_preview'] = [previews.get((doc_id, index), '')
                                for doc_id, index in zip(frame.doc_id, frame.passage_idx)]
    frame['location'] = frame.apply(
        lambda row: f"{row.passage_type} passage {row.passage_idx} @ {row.offset}", axis=1)
    frame = frame.sort_values(['doc_id', 'db', 'surface', 'passage_idx', 'offset'])
    first = frame.drop_duplicates(keys).set_index(keys)
    details = pd.DataFrame(index=first.index)
    details[f'{prefix}_section'] = first['passage_type']
    details[f'{prefix}_passage_idx'] = first['passage_idx']
    details[f'{prefix}_offset'] = first['offset']
    details[f'{prefix}_passage_preview'] = first['passage_preview']
    details[f'{prefix}_locations'] = frame.groupby(keys)['location'].agg('; '.join)
    details[f'{prefix}_occurrence_count'] = frame.groupby(keys).size()
    if prefix == 'togoid':
        details['togoid_identifier'] = first['identifier']
        details['togoid_curie'] = first['curie']
        details['togoid_identifiers_org_url'] = first['curie']
    if prefix == 'epmc':
        details['identifiers_org_url'] = first.get('raw_uri', first.get('curie', ''))
        details['europepmc_annotation_url'] = first.get('ann_id', '')
    return details.reset_index()


def document_level(result, out, all_doc_ids=None):
    """Per-paper breakdown: how many distinct accessions each doc_id has in
    each membership bucket (europepmc_only / togoid_only / both), plus the
    percentage each bucket represents of that paper's total (the three
    percentages sum to 100 per row) -- and a histogram of papers by their
    %-both (agreement) rate, so you don't need to re-derive this by hand.

    all_doc_ids (every doc_id the plan processed, not just ones with a hit)
    -- when given, papers with zero accessions on either side still get a
    row (both=togoid_only=europepmc_only=total=0, has_data=False, pct_*
    left blank since 0/0 isn't a rate). Without it, only papers with >=1
    accession on either side appear (the old behaviour)."""
    buckets = ('both', 'togoid_only', 'europepmc_only')
    counts = result.groupby(['doc_id', 'membership']).size().unstack(fill_value=0)
    for col in buckets:
        if col not in counts:
            counts[col] = 0
    counts = counts[list(buckets)]
    if all_doc_ids:
        counts = counts.reindex(sorted(set(counts.index) | set(all_doc_ids)), fill_value=0)
        counts.index.name = 'doc_id'
    counts['total'] = counts[list(buckets)].sum(axis=1)
    counts['has_data'] = counts['total'] > 0
    total_float = counts['total'].astype(float)  # float division: 0/0 -> NaN, no explicit replace needed
    for col in buckets:
        counts[f'pct_{col}'] = (counts[col] / total_float * 100).round(1)
    counts = counts.reset_index().sort_values('doc_id')
    counts.to_csv(out / 'by_document.tsv', sep='\t', index=False)

    with_data = counts[counts['has_data']]
    bin_edges = list(range(0, 101, 10))
    bin_labels = [f'{lo}-{lo + 10}%' for lo in bin_edges[:-1]]
    agreement_bucket = pd.cut(with_data['pct_both'], bins=bin_edges,
                              labels=bin_labels, include_lowest=True)
    hist = agreement_bucket.value_counts().reindex(bin_labels, fill_value=0)
    hist_df = hist.rename_axis('pct_both_bucket').reset_index(name='n_documents')
    n_no_data = len(counts) - len(with_data)
    hist_df.attrs['n_no_data'] = n_no_data
    hist_df.attrs['n_total_papers'] = len(counts)
    hist_df.to_csv(out / 'document_agreement_histogram.tsv', sep='\t', index=False)

    print(f'\nPer-document agreement histogram ({len(with_data)} papers with >=1 accession '
          f'on either side out of {len(counts)} total; bucketed by %% of that paper\'s '
          f'accessions found by both):')
    max_n = max(int(hist.max()), 1)
    for label, n in hist.items():
        bar = '#' * round(50 * n / max_n)
        print(f'  {label:>8s} | {bar} {n}')
    if n_no_data:
        print(f'  (+ {n_no_data:,} papers with zero accessions found by either side)')
    return counts, hist_df


def compare(left, right, out, plan_path=None, reconciliation_path=None, include_tables_text=False):
    keys = ['doc_id', 'db', 'surface']
    previews, all_doc_ids = passage_previews(plan_path, include_tables_text)
    result = occurrence_details(left, 'togoid', previews).merge(
        occurrence_details(right, 'epmc', previews), on=keys, how='outer', indicator=True)
    result['membership'] = result.pop('_merge').astype(str).replace(
        {'left_only': 'togoid_only', 'right_only': 'europepmc_only'})
    result['europepmc_article_url'] = result.doc_id.map(
        lambda doc_id: f'https://europepmc.org/article/PMC/{doc_id[3:]}'
        if str(doc_id).startswith('PMC') else '')

    reconciliation = load_reconciliation(reconciliation_path) if reconciliation_path else None
    if reconciliation is not None:
        result = result.merge(reconciliation, on='db', how='left')

    result.sort_values(keys).to_csv(out / 'comparison.tsv', sep='\t', index=False)
    counts = result.groupby(['db', 'membership']).size().unstack(fill_value=0)
    if reconciliation is not None:
        counts = counts.merge(reconciliation.set_index('db'), left_index=True, right_index=True, how='left')
    counts.to_csv(out / 'by_database.tsv', sep='\t')
    document_level(result, out, all_doc_ids)
    summary = {k: int((result.membership == k).sum())
               for k in ('both', 'togoid_only', 'europepmc_only')}
    summary['unit'] = 'distinct paper/database/literal accession'
    summary['note'] = 'Agreement is not accuracy. Review Europe PMC coverage and rejected rows.'
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


def merge(left, right, out):
    df = pd.concat([left, right], ignore_index=True)
    keys = ['doc_id', 'db', 'surface', 'offset']
    # Same local span from both services is one occurrence, with both provenances.
    provenance = df.groupby(keys, dropna=False).source.agg(
        lambda s: ','.join(sorted(set(s)))).rename('source')
    df['_priority'] = df.confidence.ne('high').astype(int)
    df = df.sort_values('_priority').drop_duplicates(keys).drop(columns=['source', '_priority'])
    df = df.merge(provenance.reset_index(), on=keys)
    df.to_parquet(out / 'shard_merged.parquet', index=False)
    print(f'{len(df)} deduplicated occurrences')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['compare', 'merge'])
    ap.add_argument('--togoid', required=True)
    ap.add_argument('--epmc', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--plan', help='Optional shard plan for local XML passage previews')
    ap.add_argument('--include-tables-text', action='store_true',
                     help='MUST match the --include-tables-text setting used for extraction/'
                          'normalize (on by default in run_extraction_compare.py), or every '
                          'table-sourced match silently gets no passage preview')
    ap.add_argument('--reconciliation',
                     help='Optional namespace_reconciliation.tsv (from reconcile_namespaces.py); '
                          'joined onto compare output by dataset key for registry name/URI/status')
    args = ap.parse_args()
    left, right = read_layer(args.togoid), read_layer(args.epmc)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if args.command == 'compare':
        compare(left, right, out, args.plan, args.reconciliation, args.include_tables_text)
    else:
        merge(left, right, out)


if __name__ == '__main__':
    main()
