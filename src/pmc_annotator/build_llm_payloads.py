"""
build_llm_payloads.py — 役割判定 部品(b1): doc-level LLM ペイロードビルダ + コスト見積り
                        [API を呼ばない]

role_prelim_*.tsv(to_llm=1) + windows.jsonl を doc 単位に束ね、Messages API 形の
リクエストを llm_payloads.jsonl に出力。system(ルーブリック)は prompt cache 前提で
全 doc 共通。トークン概算と(価格指定時の)コストを stderr に表示。実呼び出しは (b2)。
"""
from __future__ import annotations
import argparse, csv, json, sys
from collections import defaultdict, Counter

DEPOSIT_CLASSES = {"deposited_research_record", "deposited_entity_registry"}

# ---- ルーブリック(system, prompt cache 対象。全 doc 共通) ----
RUBRIC = """\
You classify how each referenced database identifier is used IN THIS PAPER.
Assign exactly one role per identifier:

- "generated": THIS paper's study produced the data/record and registered/deposited it.
  Signals: Data Availability / Accession Numbers sections; "deposited", "submitted",
  "we generated/sequenced", "the following data were generated", "has been deposited".
  Credit goes to this paper's authors.
- "used": a pre-existing dataset or reference resource is taken as input/material for
  this study's analysis. Signals: "downloaded/obtained/retrieved from", "reanalyzed",
  "we used data from", "publicly available data ... we compared to", aligning/mapping
  to a reference genome/assembly, or using it as the query/reference.
  Credit goes to the ORIGINAL depositor.
- "mentioned": the identifier appears but the dataset is neither produced nor consumed
  as data here. Includes: naming an entity (a gene/protein/domain by its accession),
  examples, related-work citations, a BLAST/search HIT reported as a result, database
  or tool descriptions.

Rules:
- Decide from the WINDOW TEXT. The «...» markers show the exact identifier occurrence.
- A "prior" may be given per identifier. Treat it as a weak hint only; the window text
  overrides it. If the window contradicts the prior, follow the window.
- "publicly available" is ambiguous: if the paper produced the data and made it available
  -> "generated"; if the paper used available data made by others -> "used".
- Reference-resource use (aligned/mapped to a genome, used as query/reference) -> "used".
  Merely being named or annotated as a domain/family -> "mentioned".
- evidence_span: a short quote (<= 15 words) copied from the window that justifies the role.
- confidence: 0.0-1.0.

Return ONLY a JSON object of this exact shape, no prose, no markdown fences:
{"results": [{"id": "<identifier>", "role": "generated|used|mentioned", "evidence_span": "<<=15 words from the window>", "confidence": 0.0}]}
Include exactly one results entry for every identifier in the INPUT, copying the identifier string verbatim into "id".
"""


def est_tokens(s: str) -> int:
    # 英語のざっくり近似: 約4文字/トークン
    return (len(s) + 3) // 4


def load_prelim(path):
    rows = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if r.get("to_llm") != "1":
                continue
            rows[(r["doc_id"], r["dataset"], r["id"])] = {
                "class": r.get("class", ""),
                "prior": r.get("prior", "") or "none",
                "section_type": r.get("section_type", "") or "",
                "note": r.get("note", ""),
            }
    return rows


def load_best_windows(path):
    """(doc,dataset,id)->代表窓。gen_cue>use_cue>先頭 で1つ選ぶ。"""
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
            cur = best.get(key)
            score = (2 if r.get("gen_cue") else 0) + (1 if r.get("use_cue") else 0)
            if cur is None or score > cur[0]:
                best[key] = (score, r.get("window_text", ""), r.get("section_type", ""))
    return {k: (v[1], v[2]) for k, v in best.items()}


def build_user_text(entities):
    """doc 内の対象 entity 群 -> user メッセージ本文"""
    lines = ["Classify the role of each identifier below. Return the JSON object described "
             "in the system message.\n"]
    for i, e in enumerate(entities, 1):
        lines.append(
            f'[{i}] id="{e["id"]}"  database={e["dataset"]}  class={e["class"]}  '
            f'section={e["section_type"] or "?"}  prior={e["prior"]}\n'
            f'window: {e["window"]}\n'
        )
    ids = ", ".join(f'"{e["id"]}"' for e in entities)
    lines.append(f"\nIdentifiers to classify (output keys, all required): {ids}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prelim", required=True, help="role_prelim_*.tsv")
    ap.add_argument("--windows", required=True, help="windows.jsonl")
    ap.add_argument("--out", required=True, help="出力 llm_payloads.jsonl")
    ap.add_argument("--model-upper", default="claude-sonnet-5", help="deposit 系 doc 用(仮)")
    ap.add_argument("--model-cheap", default="claude-haiku-4-5", help="derived 残余 doc 用(仮)")
    ap.add_argument("--max-window-chars", type=int, default=1500)
    ap.add_argument("--show", type=int, default=1, help="先頭 N doc の user 本文を stderr 表示")
    # 価格($/Mtok)。両方指定でコスト表示。未指定ならトークンのみ。
    ap.add_argument("--price-upper-in", type=float, default=None)
    ap.add_argument("--price-upper-out", type=float, default=None)
    ap.add_argument("--price-cheap-in", type=float, default=None)
    ap.add_argument("--price-cheap-out", type=float, default=None)
    ap.add_argument("--price-cache-mult", type=float, default=0.1,
                    help="キャッシュ読取の入力単価倍率(概算, 既定0.1)")
# argparse に追加
    ap.add_argument("--max-entities-per-doc", type=int, default=15,
                    help="1リクエスト最大entity数。超過分は同doc_idで複数payloadに分割(context溢れ防止)")
    args = ap.parse_args()

    prelim = load_prelim(args.prelim)
    windows = load_best_windows(args.windows)

    # doc 単位に集約
    by_doc = defaultdict(list)
    miss = 0
    for (doc_id, dataset, ent_id), meta in prelim.items():
        win = windows.get((doc_id, dataset, ent_id))
        if win is None:
            miss += 1
            continue
        wtext = win[0][:args.max_window_chars]
        by_doc[doc_id].append({
            "id": ent_id, "dataset": dataset, "class": meta["class"],
            "prior": meta["prior"], "section_type": meta["section_type"] or win[1],
            "window": wtext,
        })

    rubric_tok = est_tokens(RUBRIC)
    cnt = Counter()
    var_in = {"upper": 0, "cheap": 0}
    out_tok = {"upper": 0, "cheap": 0}
    n_docs = {"upper": 0, "cheap": 0}

    shown = 0
    with open(args.out, "w") as fo:
        for doc_id, all_entities in by_doc.items():
            M = args.max_entities_per_doc
            chunks = ([all_entities[i:i+M] for i in range(0, len(all_entities), M)]
                      if M > 0 else [all_entities])
            for entities in chunks:
                tier = "upper" if any(e["class"] in DEPOSIT_CLASSES for e in entities) else "cheap"
                user_text = build_user_text(entities)
                n_ent = len(entities)
                max_out = min(2048, 120 + n_ent * 60)
                payload = {
                    "doc_id": doc_id, "tier": tier, "n_entities": n_ent,
                    "expected_ids": [e["id"] for e in entities],
                    "entities": [{"id": e["id"], "dataset": e["dataset"],
                                  "class": e["class"], "prior": e["prior"]} for e in entities],
                    "system_text": RUBRIC, "user_text": user_text, "max_tokens": max_out,
                }
                fo.write(json.dumps(payload, ensure_ascii=False) + "\n")
                cnt["docs"] += 1; cnt["entities"] += n_ent
                n_docs[tier] += 1
                var_in[tier] += est_tokens(user_text); out_tok[tier] += max_out
                if shown < args.show:
                    shown += 1
                    print(f"\n===== sample payload {shown} (doc={doc_id}, tier={tier}, "
                          f"entities={n_ent}) =====", file=sys.stderr)
                    print(user_text[:2000], file=sys.stderr)

    # ---- 集計 ----
    print("\n[summary]", file=sys.stderr)
    print(f"  docs={cnt['docs']}  entities={cnt['entities']}  "
          f"window欠落(スキップ)={miss}", file=sys.stderr)
    print(f"  rubric(system,共通)≈{rubric_tok} tok", file=sys.stderr)
    for tier in ("upper", "cheap"):
        print(f"  [{tier}] docs={n_docs[tier]}  可変入力≈{var_in[tier]} tok  "
              f"出力上限≈{out_tok[tier]} tok", file=sys.stderr)

    def cost(pin, pout, var, out, nd):
        if pin is None or pout is None:
            return None
        # キャッシュ: rubric は書込1回 + 読取(nd-1)回を概算。可変入力/出力は都度。
        cached_in = rubric_tok * (1 + (nd - 1) * args.price_cache_mult) + var
        naive_in = rubric_tok * nd + var
        c_cached = cached_in / 1e6 * pin + out / 1e6 * pout
        c_naive = naive_in / 1e6 * pin + out / 1e6 * pout
        return c_naive, c_cached

    total = 0.0
    have_price = False
    for tier, pin, pout in (("upper", args.price_upper_in, args.price_upper_out),
                            ("cheap", args.price_cheap_in, args.price_cheap_out)):
        c = cost(pin, pout, var_in[tier], out_tok[tier], n_docs[tier])
        if c:
            have_price = True
            print(f"  [{tier}] コスト概算: naive=${c[0]:.2f}  prompt-cache=${c[1]:.2f}",
                  file=sys.stderr)
            total += c[1]
    if have_price:
        print(f"  合計(cache適用)≈${total:.2f}", file=sys.stderr)
    else:
        print("  (価格未指定: --price-*-in/out $/Mtok を渡すとコスト概算を表示)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
