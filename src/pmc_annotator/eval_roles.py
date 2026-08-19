"""
eval_roles.py — 役割判定 評価集計

gold_todo.tsv(人手ラベル gold_role/gold_subtag) と gold_key.tsv(モデル出力) を
sample_id で突合し、混同行列・per-role P/R/F1・rule/LLM 別精度・信頼度ビン別精度を出力。
unsure と ref_resource サブタグは分離集計。
"""
from __future__ import annotations
import argparse, csv
from collections import defaultdict, Counter

ROLES = ["generated", "used", "mentioned"]


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def pct(n, d):
    return f"{100*n/d:5.1f}%" if d else "  n/a"


def prf(cm):
    """cm[(gold,pred)] -> per-role P/R/F1 と macro/accuracy"""
    lines, macro = [], []
    total = sum(cm.values())
    correct = sum(v for (g, p), v in cm.items() if g == p)
    for r in ROLES:
        tp = cm.get((r, r), 0)
        fp = sum(v for (g, p), v in cm.items() if p == r and g != r)
        fn = sum(v for (g, p), v in cm.items() if g == r and p != r)
        P = tp / (tp + fp) if tp + fp else 0.0
        R = tp / (tp + fn) if tp + fn else 0.0
        F = 2 * P * R / (P + R) if P + R else 0.0
        support = sum(v for (g, p), v in cm.items() if g == r)
        macro.append(F)
        lines.append(f"  {r:10s}  P={P:5.3f}  R={R:5.3f}  F1={F:5.3f}  support={support}")
    lines.append(f"  ---- accuracy={correct}/{total}={pct(correct,total)}  "
                 f"macro-F1={sum(macro)/len(macro):5.3f}")
    return lines


def confusion(pairs):
    cm = Counter(pairs)
    hdr = "gold\\pred   " + "".join(f"{r:>11s}" for r in ROLES) + "   total"
    out = [hdr]
    for g in ROLES:
        row = sum(cm.get((g, p), 0) for p in ROLES)
        cells = "".join(f"{cm.get((g,p),0):>11d}" for p in ROLES)
        out.append(f"{g:10s} {cells}   {row}")
    tr = "total      " + "".join(f"{sum(cm.get((g,p),0) for g in ROLES):>11d}" for p in ROLES)
    out.append(tr)
    return out, cm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--todo", required=True, help="ラベル済み gold_todo.tsv")
    ap.add_argument("--key", required=True, help="gold_key.tsv")
    ap.add_argument("--bins", default="0.8,0.9,1.0", help="信頼度ビン境界(カンマ区切り)")
    args = ap.parse_args()

    todo = {r["sample_id"]: r for r in load(args.todo)}
    key = {r["sample_id"]: r for r in load(args.key)}

    paired, skipped = [], Counter()
    for sid, t in todo.items():
        g = (t.get("gold_role") or "").strip().lower()
        k = key.get(sid)
        if k is None:
            skipped["no_key"] += 1; continue
        if not g:
            skipped["unlabeled"] += 1; continue
        if g == "unsure":
            skipped["unsure"] += 1; continue
        if g not in ROLES:
            skipped[f"badlabel:{g}"] += 1; continue
        paired.append({
            "sid": sid, "gold": g, "pred": k["model_role"].strip().lower(),
            "source": k["role_source"], "class": t.get("class", ""),
            "conf": k.get("role_confidence", ""),
            "gold_subtag": (t.get("gold_subtag") or "").strip().lower(),
        })

    n = len(paired)
    print(f"=== 突合 {n} 件 (除外: " +
          ", ".join(f"{k}={v}" for k, v in skipped.items()) + ") ===\n")
    if n == 0:
        print("ラベル済みが0件。gold_role を埋めてから再実行してください。"); return

    # 全体混同行列
    print("【混同行列(全体: rule+LLM)】")
    lines, _ = confusion([(p["gold"], p["pred"]) for p in paired])
    print("\n".join(lines)); print()
    print("【per-role P/R/F1(全体)】")
    print("\n".join(prf(Counter((p["gold"], p["pred"]) for p in paired)))); print()

    # rule 由来 / LLM 由来 を分けて
    for src in ("rule", "llm"):
        sub = [p for p in paired if p["source"] == src]
        if not sub:
            continue
        acc = sum(1 for p in sub if p["gold"] == p["pred"])
        print(f"【{src} 由来: n={len(sub)}  accuracy={pct(acc,len(sub))}】")
        clines, _ = confusion([(p["gold"], p["pred"]) for p in sub])
        print("\n".join(clines))
        print("\n".join(prf(Counter((p["gold"], p["pred"]) for p in sub)))); print()

    # 生成⇄使用に絞った誤り(最重要)
    gu = [p for p in paired if p["gold"] in ("generated", "used") or p["pred"] in ("generated", "used")]
    conf_gu = sum(1 for p in gu if {p["gold"], p["pred"]} == {"generated", "used"})
    print(f"【生成⇄使用の取り違え】{conf_gu} 件 "
          f"(gen→used / used→gen の合計、most costly)\n")

    # クラス別 accuracy
    print("【class 別 accuracy】")
    byc = defaultdict(lambda: [0, 0])
    for p in paired:
        byc[p["class"]][0] += (p["gold"] == p["pred"]); byc[p["class"]][1] += 1
    for c in sorted(byc):
        ok, tot = byc[c]
        note = "  ※小標本" if tot < 10 else ""
        print(f"  {c:28s} {ok}/{tot} = {pct(ok,tot)}{note}")
    print()

    # 信頼度ビン別(LLM のみ)
    bounds = [float(x) for x in args.bins.split(",")]
    print(f"【信頼度ビン別 実精度(LLM由来のみ)】境界={bounds}")
    llm = [p for p in paired if p["source"] == "llm" and p["conf"] not in ("", "error")]
    def binof(v):
        for b in bounds:
            if v < b: return f"<{b}"
        return f">={bounds[-1]}"
    bybin = defaultdict(lambda: [0, 0])
    for p in llm:
        try: v = float(p["conf"])
        except ValueError: continue
        bybin[binof(v)][0] += (p["gold"] == p["pred"]); bybin[binof(v)][1] += 1
    for b in sorted(bybin):
        ok, tot = bybin[b]
        print(f"  conf {b:8s}: {ok}/{tot} = {pct(ok,tot)}")
    print()

    # 参照資源利用(ref_resource)の分離: gold=used のうちサブタグ内訳
    used_gold = [p for p in paired if p["gold"] == "used"]
    refc = Counter(p["gold_subtag"] or "(none)" for p in used_gold)
    print("【gold=used の内訳(被寄託再利用 vs 参照資源利用)】")
    for k, v in refc.most_common():
        print(f"  subtag={k}: {v}")
    print("  ※ ref_resource は最終メトリクスで被寄託データ再利用と分けて集計する材料")


if __name__ == "__main__":
    main()
