"""Adapt regex_togoid extraction parquet into the inputs verify_t2/verify_t3/build_metrics expect.

phase_regex_togoid.py writes one row per occurrence (doc_id, db, surface, offset, ...).
verify_t2.py wants unique (dataset, id) pairs; build_metrics.py wants every occurrence
as a headerless TSV (doc_id, db, surface, offset, confidence). Both are derived here
from the same parquet so the two verification-chain inputs never drift apart.
"""
import argparse
from pathlib import Path

import pandas as pd


def build(togoid_dir, mentions_out, t2_input_out):
    files = sorted(Path(togoid_dir).rglob('shard_*.parquet'))
    if not files:
        raise ValueError(f'No shard Parquet files under {togoid_dir}')
    df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)

    mentions = df[['doc_id', 'db', 'surface', 'offset', 'confidence']]
    Path(mentions_out).parent.mkdir(parents=True, exist_ok=True)
    mentions.to_csv(mentions_out, sep='\t', header=False, index=False)

    pairs = df[['db', 'surface']].drop_duplicates()
    Path(t2_input_out).parent.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(t2_input_out, sep='\t', header=False, index=False)

    print(f'{len(mentions)} mentions -> {mentions_out}')
    print(f'{len(pairs)} unique (db, surface) pairs -> {t2_input_out}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--togoid', required=True, help='regex_togoid output dir (or a single shard parquet)')
    ap.add_argument('--mentions-out', required=True, help='headerless mentions.tsv for build_metrics.py')
    ap.add_argument('--t2-input-out', required=True, help='dataset\\tid pairs for verify_t2.py verify --in')
    args = ap.parse_args()
    build(args.togoid, args.mentions_out, args.t2_input_out)


if __name__ == '__main__':
    main()
