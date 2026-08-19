"""
gene precision 調査: our_only の gene を構造的特徴で分類する.
verify_against_pubtator_v2.py と同じディレクトリに置くこと (import するため)。
"""
import argparse
import re
import sys
import json
import time
import urllib.request
import urllib.error
import unicodedata
from pathlib import Path
from collections import defaultdict, Counter

sys.path.insert(0, str(Path(__file__).parent))
from verify_against_pubtator_v2 import (
    fetch_pubtator, load_our_annotations, normalize_surface, match_surfaces,
)

PROTEIN_KEYWORDS = (
    "receptor", "channel", "factor", "kinase", "transferase", "oxidase",
    "reductase", "synthase", "synthetase", "protease", "phosphatase",
    "transporter", "antigen", "ligand", "enzyme", "subunit", "complex",
    "hormone", "peptide", "antibody", "interleukin", "cytokine",
    "dehydrogenase", "polymerase", "hydrolase", "isomerase", "ligase",
)
GENE_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")


def classify_gene(surface_raw, surface_norm, has_id):
    s = surface_norm
    raw = surface_raw
    if re.search(r"\b(\w+)[.\s]+\1\b", s):
        return "span_broken"
    if "." in s and not re.search(r"\d\.\d", s):
        return "span_broken"
    if re.match(r"^(background|the|and|with|of|in|by)\w{4,}", s):
        return "span_broken"
    if raw.startswith("-") or raw.endswith("-"):
        return "fragment"
    if len(s) <= 2:
        return "fragment"
    for kw in PROTEIN_KEYWORDS:
        if kw in s:
            return "protein_like"
    raw_stripped = raw.strip()
    if GENE_SYMBOL_RE.match(raw_stripped):
        return "gene_symbol"
    if re.match(r"^[A-Z][A-Z0-9]{1,6}-[A-Za-z0-9]{1,6}$", raw_stripped):
        return "gene_symbol"
    words = s.split()
    if words and any(w.endswith("ase") or w.endswith("ases") for w in words):
        return "protein_like"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--our-annotations", type=Path, required=True)
    ap.add_argument("--pmcids", default=None)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--output", type=Path, default=Path("gene_analysis.json"))
    args = ap.parse_args()

    if args.pmcids:
        pmcids = [p.strip() for p in args.pmcids.split(",")]
    elif args.sample:
        import duckdb
        glob = str(args.our_annotations / "shard_*.parquet")
        rows = duckdb.query(
            f"SELECT DISTINCT doc_id FROM '{glob}' USING SAMPLE {args.sample} ROWS"
        ).fetchall()
        pmcids = [r[0] for r in rows]
    else:
        print("--pmcids か --sample が必要"); sys.exit(1)

    print(f"[1] PubTator3 取得 ({len(pmcids)} 論文)...")
    ptc = fetch_pubtator(pmcids)
    print(f"  {len(ptc)} 論文")

    print(f"[2] 自前ロード...")
    ours, noise = load_our_annotations(args.our_annotations, set(pmcids))
    print(f"  {len(ours)} 論文")

    import duckdb
    glob = str(args.our_annotations / "shard_*.parquet")
    pmcid_list = "','".join(pmcids)
    raw_rows = duckdb.query(f"""
        SELECT doc_id, surface, entity_type, identifier
        FROM '{glob}' WHERE doc_id IN ('{pmcid_list}') AND entity_type='gene'
    """).fetchall()
    raw_map = {}
    for doc_id, surface, etype, identifier in raw_rows:
        sn = normalize_surface(surface)
        raw_map[(doc_id, sn)] = (surface, identifier is not None)

    common = sorted(set(ptc.keys()) & set(ours.keys()))
    print(f"[3] 共通論文: {len(common)}")

    category_counts = Counter()
    category_examples = defaultdict(list)
    category_has_id = defaultdict(lambda: [0, 0])

    for pmcid in common:
        ptc_se = {(s, e) for s, e, _ in ptc[pmcid]}
        our_se = {(s, e) for s, e, _ in ours[pmcid]}
        m = match_surfaces(ptc_se, our_se)
        for (s, e) in m["our_only"]:
            if e != "gene":
                continue
            raw, has_id = raw_map.get((pmcid, s), (s, False))
            cat = classify_gene(raw, s, has_id)
            category_counts[cat] += 1
            category_has_id[cat][1] += 1
            if has_id:
                category_has_id[cat][0] += 1
            if len(category_examples[cat]) < 15:
                category_examples[cat].append((raw, "ID" if has_id else "no-id"))

    total = sum(category_counts.values())
    print(f"\n[4] our_only gene の分類 (計 {total} 件)")
    print(f"  {'category':14s} {'count':>7s} {'pct':>6s} {'with_id':>9s}")
    valid_cats = ("protein_like", "gene_symbol")
    noise_cats = ("span_broken", "fragment")
    n_valid = n_noise = n_other = 0
    for cat in ("protein_like", "gene_symbol", "other", "fragment", "span_broken"):
        c = category_counts.get(cat, 0)
        if c == 0:
            continue
        hid, tot = category_has_id[cat]
        pct = 100 * c / total if total else 0
        idpct = 100 * hid / tot if tot else 0
        print(f"  {cat:14s} {c:>7d} {pct:>5.1f}% {idpct:>7.0f}%")
        if cat in valid_cats:
            n_valid += c
        elif cat in noise_cats:
            n_noise += c
        else:
            n_other += c

    print(f"\n[5] 評価")
    print(f"  妥当 (protein_like+gene_symbol): {n_valid} ({100*n_valid/total:.1f}%)")
    print(f"  ノイズ (span_broken+fragment):   {n_noise} ({100*n_noise/total:.1f}%)")
    print(f"  要目視 (other):                  {n_other} ({100*n_other/total:.1f}%)")

    print(f"\n[6] カテゴリ別サンプル")
    for cat in ("protein_like", "gene_symbol", "other", "fragment", "span_broken"):
        if category_examples[cat]:
            print(f"\n  [{cat}]:")
            for raw, idflag in category_examples[cat]:
                print(f"    {raw!r:40s} ({idflag})")

    out = {
        "total_our_only_gene": total,
        "category_counts": dict(category_counts),
        "n_valid": n_valid, "n_noise": n_noise, "n_other": n_other,
        "examples": {k: v for k, v in category_examples.items()},
    }
    with open(args.output, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[7] {args.output} に出力")


if __name__ == "__main__":
    main()
