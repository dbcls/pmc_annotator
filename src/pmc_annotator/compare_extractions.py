"""Merge aligned annotation layers or compare paper/database/literal-accession sets."""
import argparse
import json
from pathlib import Path
import pandas as pd


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


def compare(left, right, out):
    keys = ['doc_id', 'db', 'surface']
    result = left[keys].drop_duplicates().merge(
        right[keys].drop_duplicates(), on=keys, how='outer', indicator=True)
    result['membership'] = result.pop('_merge').astype(str).replace(
        {'left_only': 'togoid_only', 'right_only': 'europepmc_only'})
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
    args = ap.parse_args()
    left, right = read_layer(args.togoid), read_layer(args.epmc)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    {'compare': compare, 'merge': merge}[args.command](left, right, out)


if __name__ == '__main__':
    main()
