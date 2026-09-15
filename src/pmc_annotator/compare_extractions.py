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


def passage_previews(plan_path):
    if not plan_path:
        return {}
    plan = json.loads(Path(plan_path).read_text())
    parser, previews = JATSParser(), {}
    for shard in plan['shards']:
        for filename in shard['files']:
            doc = parser.parse(Path(filename))
            if doc is not None:
                for index, passage in enumerate(doc.passages):
                    previews[(doc.id, index)] = passage.text[:300]
    return previews


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
    if prefix == 'epmc':
        details['identifiers_org_url'] = first.get('raw_uri', first.get('curie', ''))
        details['europepmc_annotation_url'] = first.get('ann_id', '')
    return details.reset_index()


def compare(left, right, out, plan_path=None):
    keys = ['doc_id', 'db', 'surface']
    previews = passage_previews(plan_path)
    result = occurrence_details(left, 'togoid', previews).merge(
        occurrence_details(right, 'epmc', previews), on=keys, how='outer', indicator=True)
    result['membership'] = result.pop('_merge').astype(str).replace(
        {'left_only': 'togoid_only', 'right_only': 'europepmc_only'})
    result['europepmc_article_url'] = result.doc_id.map(
        lambda doc_id: f'https://europepmc.org/article/PMC/{doc_id[3:]}'
        if str(doc_id).startswith('PMC') else '')
    result.sort_values(keys).to_csv(out / 'comparison.tsv', sep='\t', index=False)
    counts = result.groupby(['db', 'membership']).size().unstack(fill_value=0)
    counts.to_csv(out / 'by_database.tsv', sep='\t')
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
    args = ap.parse_args()
    left, right = read_layer(args.togoid), read_layer(args.epmc)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if args.command == 'compare':
        compare(left, right, out, args.plan)
    else:
        merge(left, right, out)


if __name__ == '__main__':
    main()
