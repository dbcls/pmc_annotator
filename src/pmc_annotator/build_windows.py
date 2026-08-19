"""
build_windows.py — 役割判定(step 3)用の文脈窓ビルダ  [v2]

v2 変更点:
- GEN 正規表現を寄託動詞に限定(中立ラベル "accession numbers" の誤爆を除去)
- make_window をリスト/表対応に刷新(列挙=導入節+局所項目、フラット表=窓なし+flag)
- --only-class / --only-db フィルタ、out_of_scope スキップを追加
"""
from __future__ import annotations
import argparse, csv, json, re, sys
from collections import defaultdict, Counter
from pathlib import Path

import pyarrow.dataset as ds

try:
    import pysbd
except ImportError:
    sys.exit("pysbd が必要です: pip install pysbd")

from pmc_annotator.preprocess import JATSParser

# --- 寄託(生成)の手掛かり: 寄託動詞に限定。中立ラベル accession numbers は入れない ---
GEN = re.compile(
    r"\b("
    r"deposit(ed|s|ing)?"
    r"|submitt(ed|ing)"
    r"|has been deposited|have been (deposited|submitted|assigned|made available|generated)"
    r"|were (generated|deposited|created|submitted|produced)"
    r"|assigned the accession"
    r"|data (are|have been|were) (available|deposited)"
    r"|we (deposited|submitted|generated|sequenced|produced|created)"
    r"|the following (data|datasets?|sequences?|samples?) (were|are)"
    r")\b", re.I)
# --- 使用(再利用)の手掛かり ---
USE = re.compile(
    r"\b("
    r"download(ed)?|obtained from|retrieved from|re-?analyz(ed|es|e|ing)"
    r"|we (used|obtained|retrieved|downloaded)|publicly available|taken from"
    r"|acquired from|datasets? from|were obtained|available (at|from) (geo|sra|arrayexpress|ena|ncbi)"
    r")\b", re.I)

# 窓モードの閾値
LIST_MIN = 400        # 含有文がこれ以上長ければ列挙とみなす
HEAD_MAX = 220        # 列挙の導入節として先頭から取る最大文字
FLAT_MAXRUN = 34      # 空白なし英数字連続がこれを超えたらフラット表
FLAT_SPACE_RATIO = 0.06


def xml_path_for(doc_id: str, xml_root: Path):
    if not doc_id.startswith("PMC"):
        return None
    try:
        n = int(doc_id[3:])
    except ValueError:
        return None
    d = xml_root / f"PMC{n // 1_000_000:03d}xxxxxx"
    for ext in (".xml", ".nxml"):
        p = d / f"{doc_id}{ext}"
        if p.exists():
            return p
    return None


def load_confirmed(path):
    by_doc = defaultdict(list)
    with open(path, newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        for row in r:
            by_doc[row["doc_id"]].append(row)
    return by_doc


def load_mentions(parquet_dir, doc_ids, over):
    # ディレクトリには *.parquet.done の 0byte 完了マーカーが混在するので、
    # 実体の *.parquet(かつ非0byte)だけを明示的に渡す
    files = sorted(str(p) for p in Path(parquet_dir).glob("*.parquet") if p.stat().st_size > 0)
    if not files:
        sys.exit(f"[parquet] *.parquet が見つかりません: {parquet_dir}")
    print(f"[parquet] {len(files)} ファイルを読み込み", file=sys.stderr)
    dset = ds.dataset(files, format="parquet")
    names = set(dset.schema.names)

    def pick(cands, label, required=True):
        for c in cands:
            if c in names:
                return c
        if required:
            sys.exit(f"[schema] 列が見つかりません({label}): 候補={cands} 実列={sorted(names)}")
        return None

    c_doc  = over.get("doc")  or pick(["doc_id"], "doc_id")
    c_pidx = over.get("pidx") or pick(["passage_idx"], "passage_idx")
    c_off  = over.get("off")  or pick(["offset"], "offset")
    c_len  = over.get("len")  or pick(["length"], "length")
    c_db   = over.get("db")   or pick(["db", "dataset", "database"], "db")
    c_surf = over.get("surf") or pick(["surface"], "surface")
    c_curie = over.get("curie") if over.get("curie") in names else ("curie" if "curie" in names else None)
    c_ident = over.get("ident") if over.get("ident") in names else ("identifier" if "identifier" in names else None)

    cols = list(dict.fromkeys([c_doc, c_pidx, c_off, c_len, c_db, c_surf,
                               *(c for c in (c_curie, c_ident) if c)]))
    tbl = dset.to_table(columns=cols, filter=ds.field(c_doc).isin(list(doc_ids)))
    df = tbl.to_pandas()
    print(f"[schema] parquet 使用列: doc={c_doc} passage_idx={c_pidx} offset={c_off} "
          f"length={c_len} db={c_db} surface={c_surf} id照合={[c for c in (c_surf,c_curie,c_ident) if c]}",
          file=sys.stderr)

    index = defaultdict(list)
    for t in df.itertuples(index=False):
        d = dict(t._asdict())
        idvals = {str(d[c_surf])}
        if c_curie: idvals.add(str(d[c_curie]))
        if c_ident: idvals.add(str(d[c_ident]))
        index[(str(d[c_doc]), str(d[c_db]))].append({
            "passage_idx": int(d[c_pidx]),
            "offset": int(d[c_off]),
            "length": int(d[c_len]),
            "surface": str(d[c_surf]),
            "idvals": idvals,
        })
    return index


def sentence_spans(text, seg):
    spans, cur = [], 0
    for s in seg.segment(text):
        if not s:
            continue
        i = text.find(s, cur)
        if i < 0:
            i = text.find(s)
            if i < 0:
                continue
        spans.append((i, i + len(s)))
        cur = i + len(s)
    return spans or [(0, len(text))]


def locate(passages, pidx, offset, length, surface):
    """(passage_index, within, align_ok) を返す。ズレ時は surface 再探索へ。"""
    if 0 <= pidx < len(passages):
        p = passages[pidx]
        within = offset - p.offset
        if within >= 0 and p.text[within:within + length] == surface:
            return pidx, within, True
        j = p.text.find(surface)
        if j >= 0:
            return pidx, j, False
    for i, p in enumerate(passages):
        j = p.text.find(surface)
        if j >= 0:
            return i, j, False
    if 0 <= pidx < len(passages):
        p = passages[pidx]
        return pidx, max(0, min(offset - p.offset, max(0, len(p.text) - 1))), False
    return None, None, False


def is_flat_table(s: str) -> bool:
    """区切り無しの英数字スープ(フラット化された表)か判定"""
    if len(s) < 40:
        return False
    space_ratio = s.count(" ") / len(s)
    run = mx = 0
    for ch in s:
        if ch == " ":
            run = 0
        else:
            run += 1
            if run > mx:
                mx = run
    return space_ratio < FLAT_SPACE_RATIO and mx > FLAT_MAXRUN


def _mark(w, s0, s1):
    if 0 <= s0 <= s1 <= len(w):
        return w[:s0] + "«" + w[s0:s1] + "»" + w[s1:]
    return w


def make_window(text, within, length, k, max_chars, spans, lead_in):
    """(window_text, flags) を返す。flags={list_item, flat_table}"""
    s0, s1 = within, within + length
    si = next((i for i, (a, b) in enumerate(spans) if a <= within < b), None)
    if si is None:
        si = min(range(len(spans)), key=lambda j: abs(spans[j][0] - within))
    sa, sb = spans[si]
    flags = {"list_item": False, "flat_table": False}

    # (a) フラット表: 言語的窓を作らず局所スライス + フラグ
    neigh = text[max(sa, s0 - 80): min(sb, s1 + 80)]
    if is_flat_table(neigh):
        lo, hi = max(0, s0 - 60), min(len(text), s1 + 60)
        flags["flat_table"] = flags["list_item"] = True
        w, inc = _mark(text[lo:hi], s0 - lo, s1 - lo), lo
    # (b) 列挙(巨大文): 導入節 + 局所項目
    elif (sb - sa) > LIST_MIN:
        flags["list_item"] = True
        half = max(150, (max_chars - HEAD_MAX) // 2)
        lo, hi = max(sa, s0 - half), min(sb, s1 + half)
        local = _mark(text[lo:hi], s0 - lo, s1 - lo)
        if s0 <= sa + HEAD_MAX:            # surface が導入節付近: 局所のみ
            w, inc = local, lo
        else:
            head = text[sa: sa + HEAD_MAX].strip()
            w, inc = (head + " […] " + local), sa
    # (c) 通常: 前後 K 文
    else:
        lo = spans[max(0, si - k)][0]
        hi = spans[min(len(spans) - 1, si + k)][1]
        if hi - lo > max_chars:
            half = max(0, (max_chars - (s1 - s0)) // 2)
            lo, hi = max(lo, s0 - half), min(hi, s1 + half)
        w, inc = _mark(text[lo:hi], s0 - lo, s1 - lo), lo

    # 寄託リストのリード文回収: 窓に生成語が無く、passage 先頭文に生成語があり未包含なら前置
    if lead_in and not GEN.search(w):
        ha, hb = spans[0]
        if hb <= inc and GEN.search(text[ha:hb]):
            w = text[ha:hb].strip() + " […] " + w
    return w, flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage", required=True, help="usage_long.tsv")
    ap.add_argument("--parquet-dir", required=True, help="accession_annotations_togoid ディレクトリ")
    ap.add_argument("--xml-root", required=True, help="PMC_xml ルート")
    ap.add_argument("--out", required=True, help="出力 windows.jsonl")
    ap.add_argument("--k", type=int, default=1, help="前後文数")
    ap.add_argument("--max-chars", type=int, default=1500)
    ap.add_argument("--per-mention", action="store_true", help="entity ごと1行でなく全 mention を出力")
    ap.add_argument("--no-lead-in", action="store_true", help="寄託リストのリード文前置を無効化")
    ap.add_argument("--only-class", default=None, help="カンマ区切り: この class の行のみ処理")
    ap.add_argument("--only-db", default=None, help="カンマ区切り部分一致: dataset がこれを含む行のみ")
    ap.add_argument("--limit", type=int, default=0, help=">0 なら先頭 N doc だけ処理(検証用)")
    for key in ("doc", "pidx", "off", "len", "db", "surf", "curie", "ident"):
        ap.add_argument(f"--col-{key}", dest=f"col_{key}", default=None)
    args = ap.parse_args()

    over = {k: getattr(args, f"col_{k}") for k in ("doc", "pidx", "off", "len", "db", "surf", "curie", "ident")
            if getattr(args, f"col_{k}")}

    by_doc = load_confirmed(args.usage)

    # フィルタ(--only-class / --only-db)
    only_class = set(args.only_class.split(",")) if args.only_class else None
    only_db = args.only_db.split(",") if args.only_db else None
    if only_class or only_db:
        for d in list(by_doc):
            kept = [r for r in by_doc[d]
                    if (only_class is None or r.get("class") in only_class)
                    and (only_db is None or any(s in r["dataset"] for s in only_db))]
            if kept:
                by_doc[d] = kept
            else:
                del by_doc[d]

    doc_ids = list(by_doc.keys())
    if args.limit:
        doc_ids = doc_ids[:args.limit]
    print(f"[info] confirmed doc(フィルタ後): {len(by_doc)} / 処理対象: {len(doc_ids)}", file=sys.stderr)
    if not doc_ids:
        sys.exit("[info] 対象 doc が0件です(フィルタ条件を確認)")

    index = load_mentions(args.parquet_dir, set(doc_ids), over)
    seg = pysbd.Segmenter(language="en", clean=False)
    parser = JATSParser(include_captions=True, include_tables_text=False)

    cnt = Counter()
    with open(args.out, "w") as out:
        for di, doc_id in enumerate(doc_ids, 1):
            xmlp = xml_path_for(doc_id, Path(args.xml_root))
            if xmlp is None:
                cnt["doc_no_xml"] += 1
                continue
            doc = parser.parse(xmlp)
            if doc is None or not doc.passages:
                cnt["doc_parse_fail"] += 1
                continue
            passages = doc.passages
            span_cache = {}

            for row in by_doc[doc_id]:
                if row.get("class") == "out_of_scope":
                    cnt["entity_out_of_scope"] += 1
                    continue
                dataset, ent_id = row["dataset"], row["id"]
                mentions = [m for m in index.get((doc_id, dataset), []) if ent_id in m["idvals"]]
                cnt["entity_total"] += 1
                if not mentions:
                    cnt["entity_unmatched"] += 1
                    out.write(json.dumps({
                        "doc_id": doc_id, "dataset": dataset, "id": ent_id,
                        "class": row.get("class"), "matched": False,
                        "n_in_doc": row.get("n_in_doc"),
                    }, ensure_ascii=False) + "\n")
                    continue

                recs = []
                for m in mentions:
                    pidx, within, ok = locate(passages, m["passage_idx"], m["offset"], m["length"], m["surface"])
                    if pidx is None:
                        cnt["mention_locate_fail"] += 1
                        continue
                    p = passages[pidx]
                    if pidx not in span_cache:
                        span_cache[pidx] = sentence_spans(p.text, seg)
                    win, wflags = make_window(p.text, within, m["length"], args.k, args.max_chars,
                                              span_cache[pidx], lead_in=not args.no_lead_in)
                    recs.append({
                        "doc_id": doc_id, "dataset": dataset, "id": ent_id,
                        "class": row.get("class"),
                        "section_type": p.infon.get("section_type"),
                        "passage_idx": pidx, "within": within, "surface": m["surface"],
                        "n_in_doc": row.get("n_in_doc"), "n_matched": len(mentions),
                        "align_ok": ok,
                        "gen_cue": bool(GEN.search(win)), "use_cue": bool(USE.search(win)),
                        "list_item": wflags["list_item"], "flat_table": wflags["flat_table"],
                        "picked_from": m["offset"], "matched": True,
                        "window_text": win,
                    })
                    cnt["align_ok" if ok else "align_drift"] += 1
                if not recs:
                    continue
                if args.per_mention:
                    for r in recs:
                        out.write(json.dumps(r, ensure_ascii=False) + "\n")
                else:
                    # 案A: 生成語 > 使用語 > 先頭出現 で1件選ぶ。フラット表は最後の手段に回す
                    ranked = [r for r in recs if not r["flat_table"]] or recs
                    best = (next((r for r in ranked if r["gen_cue"]), None)
                            or next((r for r in ranked if r["use_cue"]), None)
                            or ranked[0])
                    out.write(json.dumps(best, ensure_ascii=False) + "\n")

    print("[summary] " + "  ".join(f"{k}={v}" for k, v in sorted(cnt.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
