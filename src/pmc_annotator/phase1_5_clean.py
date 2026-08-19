"""
Phase 1.5: アノテーション表層形のクリーン化 (span境界エラーの補正).
Phase 1 の annotations Parquet を読み、surface のスパン崩れを補正。
offset/length も連動更新する。
"""
from __future__ import annotations
import argparse
import re
import json
import time
from pathlib import Path
from collections import Counter


TRAILING_CHARS = ".,;:"
LEADING_CHARS = ".,;:-"

NUM_UPPER_RE = re.compile(r"(\d)([A-Z][a-z]{1,})")
LOWER_UPPER_RE = re.compile(r"([a-z]{3,})([A-Z][a-z]{2,})")
DUP_WORD_RE = re.compile(r"^(\w+)[.\s]+\1$", re.IGNORECASE)


def clean_surface(surface):
    original = surface
    s = surface
    left = 0
    right = 0
    rule = "none"

    # 5. 重複語除去 (cox.cox → cox)
    m = DUP_WORD_RE.match(s)
    if m:
        word = m.group(1)
        new_len = len(word)
        right = len(s) - new_len
        s = word
        rule = "dup_word"
        if len(s) >= 2:
            return s, left, right, rule
        else:
            return original, 0, 0, "none"

    # 3+4. 連結語の分離
    m = NUM_UPPER_RE.search(s)
    if m:
        cut = m.start(2)
        right = len(s) - cut
        s = s[:cut]
        rule = "num_upper_split"
    else:
        m = LOWER_UPPER_RE.search(s)
        if m:
            tail = m.group(2)
            COMMON_WORDS = ("The", "This", "These", "Those", "Many", "Most",
                            "And", "But", "However", "Although", "While",
                            "Here", "There", "When", "Where", "Our", "We",
                            "In", "Of", "To", "Is", "Are", "Was", "Were")
            if tail in COMMON_WORDS or any(tail.startswith(w) for w in COMMON_WORDS):
                cut = m.start(2)
                right = len(s) - cut
                s = s[:cut]
                rule = "lower_upper_split"

    # 1. 末尾記号除去
    before = len(s)
    s_stripped = s.rstrip(TRAILING_CHARS)
    if len(s_stripped) < before:
        right += before - len(s_stripped)
        s = s_stripped
        if rule == "none":
            rule = "trailing_punct"

    # 2. 先頭記号除去
    before = len(s)
    s_stripped = s.lstrip(LEADING_CHARS)
    if len(s_stripped) < before:
        left += before - len(s_stripped)
        s = s_stripped
        if rule == "none":
            rule = "leading_punct"

    # 安全装置
    if len(s.strip()) <= 1:
        return original, 0, 0, "none"
    if s == original:
        return original, 0, 0, "none"
    return s, left, right, rule


def process_shard(shard_path, out_path, dry_run=False):
    import pandas as pd
    df = pd.read_parquet(shard_path)
    stats = Counter()
    stats["total"] = len(df)
    if "surface" not in df.columns:
        return {"error": "no surface column"}

    new_surfaces = []
    new_offsets = []
    new_lengths = []
    for row in df.itertuples(index=False):
        surface = row.surface
        offset = row.offset
        length = row.length
        cleaned, left, right, rule = clean_surface(surface)
        stats[rule] += 1
        if rule != "none":
            stats["cleaned_total"] += 1
            new_surfaces.append(cleaned)
            new_offsets.append(offset + left)
            new_lengths.append(length - left - right)
        else:
            new_surfaces.append(surface)
            new_offsets.append(offset)
            new_lengths.append(length)

    if not dry_run:
        df["surface"] = new_surfaces
        df["offset"] = new_offsets
        df["length"] = new_lengths
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_path, index=False, compression="zstd")
        marker = out_path.with_suffix(out_path.suffix + ".done")
        marker.touch()
    return dict(stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--shard", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    in_ann = args.input_dir / "annotations"
    out_ann = args.output_dir / "annotations"

    if args.shard:
        shards = [in_ann / f"shard_{args.shard}.parquet"]
    else:
        shards = sorted(in_ann.glob("shard_*.parquet"))
    print(f"[phase1.5] {len(shards)} shards, dry_run={args.dry_run}")

    total_stats = Counter()
    t0 = time.time()
    for i, sp in enumerate(shards):
        out_path = out_ann / sp.name
        done = out_path.with_suffix(out_path.suffix + ".done")
        if not args.dry_run and done.exists():
            continue
        st = process_shard(sp, out_path, dry_run=args.dry_run)
        for k, v in st.items():
            total_stats[k] += v
        if (i + 1) % 20 == 0 or args.shard:
            print(f"  [{i+1}/{len(shards)}] {sp.name}: "
                  f"cleaned {st.get('cleaned_total', 0)}/{st.get('total', 0)}")

    print(f"\n[phase1.5] DONE in {time.time()-t0:.1f}s")
    print(f"  total annotations: {total_stats['total']:,}")
    print(f"  cleaned: {total_stats['cleaned_total']:,} "
          f"({100*total_stats['cleaned_total']/max(total_stats['total'],1):.2f}%)")
    print(f"\n  rule breakdown:")
    for rule in ("trailing_punct", "leading_punct", "num_upper_split",
                 "lower_upper_split", "dup_word"):
        c = total_stats.get(rule, 0)
        if c:
            print(f"    {rule:20s}: {c:,}")

    if not args.dry_run:
        print(f"\n  NOTE: documents/ は変更不要:")
        print(f"  ln -s {args.input_dir.resolve()}/documents {args.output_dir.resolve()}/documents")


if __name__ == "__main__":
    main()
