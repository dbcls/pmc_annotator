"""
make_gold_sample.py — 役割判定 評価: 層化ゴールド標本の抽出

usage_roles.tsv を (role_source × class × role) で層化し、観測比率に比例配分
(+希少だが重要なセルの最小フロア)で N 件抽出。ラベラーがモデル出力を見ずに
付けられるよう gold_todo.tsv(モデル出力伏せ)と gold_key.tsv(照合用)を分離出力。
"""
from __future__ import annotations
import argparse, csv, json, random
from collections import defaultdict, Counter


def load_best_windows(path):
    best = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if not r.get("matched"):
                continue
            key = (r["doc_id"], r["dataset"], r["id"])
            score = (2 if r.get("gen_cue") else 0) + (1 if r.get("use_cue") else 0)
            cur = best.get(key)
            if cur is None or score > cur[0]:
                best[key] = (score, r.get("window_text", ""), r.get("section_type", ""))
    return {k: (v[1], v[2]) for k, v in best.items()}


def allocate(counts, n, min_per_cell, cap_per_cell):
    total = sum(counts.values())
    raw = {k: n * c / total for k, c in counts.items()}
    alloc = {k: int(raw[k]) for k in raw}
    rem = n - sum(alloc.values())
    for k, _ in sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:max(0, rem)]:
        alloc[k] += 1
    for k, c in counts.items():
        a = min(c, max(alloc[k], min_per_cell))       # 最小フロア
        if cap_per_cell > 0:
            a = min(a, cap_per_cell)                   # 上限(自明セルの暴走抑制)
        alloc[k] = a
    return alloc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage-roles", required=True)
    ap.add_argument("--windows", required=True)
    ap.add_argument("--out-todo", default="gold_todo.tsv")
    ap.add_argument("--out-key", default="gold_key.tsv")
    ap.add_argument("--n", type=int, default=250)
    ap.add_argument("--min-per-cell", type=int, default=15,
                    help="希少だが重要なセルの最小件数(在庫上限)。0で厳密比例")
    ap.add_argument("--cap-per-cell", type=int, default=0,
                    help="1セルの最大件数(0=無制限)。自明な大セル(rule:mentioned)の抑制用")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    rows = [r for r in csv.DictReader(open(args.usage_roles), delimiter="\t")
            if r["role"] in ("generated", "used", "mentioned")]     # error/missing/skip 除外
    windows = load_best_windows(args.windows)

    strata = defaultdict(list)
    for r in rows:
        strata[(r["role_source"], r["class"], r["role"])].append(r)
    counts = {k: len(v) for k, v in strata.items()}
    alloc = allocate(counts, args.n, args.min_per_cell, args.cap_per_cell)

    picked = []
    for k, lst in strata.items():
        picked.extend(random.sample(lst, min(alloc[k], len(lst))))
    random.shuffle(picked)

    with open(args.out_todo, "w", newline="") as ft, open(args.out_key, "w", newline="") as fk:
        wt = csv.writer(ft, delimiter="\t"); wk = csv.writer(fk, delimiter="\t")
        wt.writerow(["sample_id", "doc_id", "dataset", "id", "class", "section_type",
                     "window_text", "gold_role", "gold_subtag", "gold_notes"])
        wk.writerow(["sample_id", "role_source", "model_role", "role_confidence",
                     "evidence_span", "prior_or_note", "model"])
        for i, r in enumerate(picked, 1):
            sid = f"G{i:04d}"
            win = windows.get((r["doc_id"], r["dataset"], r["id"]), ("", ""))
            wt.writerow([sid, r["doc_id"], r["dataset"], r["id"], r["class"],
                         win[1], win[0], "", "", ""])
            wk.writerow([sid, r["role_source"], r["role"], r.get("role_confidence", ""),
                         r.get("evidence_span", ""), r.get("note", ""), r.get("model", "")])

    print("[strata allocation] (role_source, class, role): 在庫 -> 抽出", flush=True)
    pc = Counter((r["role_source"], r["class"], r["role"]) for r in picked)
    for k in sorted(counts):
        print(f"  {k}: {counts[k]} -> {pc[k]}")
    print(f"[total] pool={len(rows)}  sampled={len(picked)}  -> {args.out_todo}, {args.out_key}")


if __name__ == "__main__":
    main()
