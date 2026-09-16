"""Filter regex_togoid extraction output down to existence-confirmed accessions.

compare_extractions.py normally compares the raw regex_togoid parquet --
every syntactic pattern match, unverified -- against Europe PMC. This
writes a filtered copy containing only rows whose (db, surface) pair has
status='confirmed' in a verified.tsv (the output of the
verify_t2 -> verify_t3 -> verify_idorg chain), so a downstream compare
measures agreement on data we've actually confirmed exists, not on
unverified extraction noise (including any false positives verification
hasn't had a chance to catch).

  python3 filter_verified_extraction.py \\
      --togoid regex_togoid/ --verified verified.idorg.tsv --output regex_togoid_verified/
"""
import argparse
from pathlib import Path

import pandas as pd


def load_confirmed_pairs(verified_path):
    df = pd.read_csv(verified_path, sep='\t', dtype=str)
    confirmed = df[df.status == 'confirmed']
    return set(zip(confirmed.dataset, confirmed.id))


def filter_extraction(togoid_dir, confirmed_pairs, output_dir):
    files = sorted(Path(togoid_dir).rglob('shard_*.parquet'))
    if not files:
        raise ValueError(f'No shard Parquet files under {togoid_dir}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    total_in = total_out = 0
    for f in files:
        df = pd.read_parquet(f)
        total_in += len(df)
        keep = pd.Series(list(zip(df.db, df.surface)), index=df.index).isin(confirmed_pairs)
        kept = df[keep]
        total_out += len(kept)
        kept.to_parquet(output_dir / f.name, index=False, compression='zstd')
    return total_in, total_out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--togoid', required=True, help='regex_togoid output dir')
    ap.add_argument('--verified', required=True, help='verified.tsv (status column, from verify_*)')
    ap.add_argument('--output', required=True, help='dir to write the filtered shard parquet into')
    args = ap.parse_args()
    confirmed = load_confirmed_pairs(args.verified)
    total_in, total_out = filter_extraction(args.togoid, confirmed, args.output)
    print(f'{total_out:,} / {total_in:,} extracted accessions kept (status=confirmed) -> {args.output}')


if __name__ == '__main__':
    main()
