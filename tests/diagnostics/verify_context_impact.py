"""
Phase 3a: linker の文脈影響検証ベンチ

二段構え設計 (Phase 3b: ユニーク表層形に対するlinker一括処理) の前提として、
"文脈あり" のlinker結果と "文脈なし" のlinker結果が実質的に一致するかを定量検証する。

検証手順:
  1. 複数論文をHunFlair2でフル処理（NER + 文脈ありlinker） → 基準結果
  2. NER結果から (surface_form, entity_type) のユニーク集合を抽出
  3. 各ユニーク表層形について、ダミーSentence("表層形そのもの")にspanを置き
     文脈なしlinker予測
  4. 文脈あり / 文脈なし の identifier 一致率を集計
     - 全体一致率
     - エンティティタイプ別
     - 表層形長さ別
     - 出現頻度別
  5. 不一致事例を質的に分析（同義語間揺れ vs 致命的相違）

使い方:
  python tests/verify_context_impact.py \
      --device cuda:2 \
      --xml-dir /home/yayamamo/PMC_xml/PMC000xxxxxx \
      --max-docs 10 \
      --output context_impact.parquet
"""
import sys
import time
import argparse
from pathlib import Path
from collections import Counter, defaultdict

_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pmc_annotator.preprocess import JATSParser, iter_jats_files


def collect_docs(xml_dir: Path, max_docs: int) -> list:
    """前処理済みDocumentを集める"""
    parser = JATSParser()
    docs = []
    for xml_path in iter_jats_files(xml_dir):
        doc = parser.parse(xml_path)
        if doc is None:
            continue
        n_chars = sum(len(p.text) for p in doc.passages)
        if n_chars < 1000:
            continue
        docs.append(doc)
        if len(docs) >= max_docs:
            break
    return docs


def step1_context_aware_linking(docs, device: str) -> list:
    """各論文をフル処理 (NER + 文脈ありlinker)、アノテーション全件を取得"""
    print(f"\n[Step 1] 文脈ありlinker 基準結果の作成")
    from pmc_annotator.annotate_hunflair import HunFlairAnnotator
    annotator = HunFlairAnnotator(device=device, enable_linkers=True)
    annotator.load()
    print(f"  HunFlair2 ロード完了")

    all_anns = []
    for i, doc in enumerate(docs):
        t0 = time.time()
        doc = annotator(doc)
        dt = time.time() - t0
        n = sum(len(p.annotations) for p in doc.passages)
        print(f"  [{i+1}/{len(docs)}] {doc.id}: {n} anns ({dt:.1f}s)")
        for p in doc.passages:
            for a in p.annotations:
                all_anns.append({
                    "doc_id": doc.id,
                    "passage_idx": doc.passages.index(p),  # 簡易
                    "surface": a.text,
                    "entity_type": a.entity_type,
                    "identifier_ctx": a.identifiers[0] if a.identifiers else None,
                    "offset": a.offset,
                })
    return all_anns, annotator


def step2_unique_surfaces(all_anns: list) -> dict:
    """ユニーク (surface, entity_type) 集合と頻度を作る"""
    print(f"\n[Step 2] ユニーク表層形の集計")
    counter = Counter()
    for ann in all_anns:
        if ann["entity_type"] == "cell_line":
            continue  # cell_line には linker なし
        counter[(ann["surface"], ann["entity_type"])] += 1
    print(f"  ユニーク (surface, entity_type) 数: {len(counter)}")
    print(f"  総アノテーション (cell_line除く): {sum(counter.values())}")
    print(f"  圧縮率: {len(counter) / sum(counter.values()):.2%}")
    # 頻度上位を表示
    print(f"\n  頻度上位10件:")
    for (surf, etype), cnt in counter.most_common(10):
        print(f"    {cnt:4d}x  [{etype:9s}] {surf!r}")
    return counter


def step3_context_free_linking(counter: dict, annotator) -> dict:
    """各ユニーク表層形を「単独文Sentence」として linker に投げる"""
    print(f"\n[Step 3] 文脈なし linker 予測")
    from flair.data import Sentence, Span, Label

    # entity_type -> HunFlair2 のNERタグ名 (大文字始まり) への変換
    INV_ENTITY_TYPE = {
        "gene": "Gene", "disease": "Disease",
        "chemical": "Chemical", "species": "Species",
    }
    LINKER_NAMES = {
        "gene": "gene-linker", "disease": "disease-linker",
        "chemical": "chemical-linker", "species": "species-linker",
    }
    ID_PREFIX = {"gene": "NCBIGene:", "species": "NCBITaxon:"}

    # 各エンティティタイプごとに表層形をバッチ化
    by_type = defaultdict(list)
    for (surf, etype), _cnt in counter.items():
        if etype not in INV_ENTITY_TYPE:
            continue
        by_type[etype].append(surf)

    results = {}  # (surface, entity_type) -> identifier_ctxfree
    for etype, surfaces in by_type.items():
        print(f"  [{etype}] {len(surfaces)} 件...")
        t0 = time.time()

        # 表層形ごとに Sentence を作り、span をマニュアルで配置
        sentences = []
        valid_idx = []  # tokenize失敗等で空 span になった場合のスキップ
        for i, surf in enumerate(surfaces):
            sent = Sentence(surf)
            if len(sent.tokens) == 0:
                continue  # tokenize失敗
            # 全トークンを覆う span を作って NER ラベルを付与
            span = sent[0:len(sent.tokens)]
            span.add_label("ner", INV_ENTITY_TYPE[etype], score=1.0)
            sentences.append(sent)
            valid_idx.append(i)

        # linker を実行
        linker = annotator._linkers.get(etype)
        if linker is None:
            print(f"    linker未ロード、スキップ")
            continue
        try:
            linker.predict(sentences)
        except Exception as e:
            print(f"    linker predict失敗: {e}")
            continue

        # 結果取り出し
        n_linked = 0
        for sent, i in zip(sentences, valid_idx):
            spans = sent.get_spans("ner")
            if not spans:
                continue
            sp = spans[0]
            link_label = sp.get_label("link")
            if link_label and link_label.value and link_label.value != "O":
                raw = link_label.value.split("/")[0].strip()
                prefix = ID_PREFIX.get(etype, "")
                if prefix and not raw.startswith(prefix.rstrip(":")):
                    raw = prefix + raw
                results[(surfaces[i], etype)] = raw
                n_linked += 1
        dt = time.time() - t0
        print(f"    {n_linked}/{len(surfaces)} 件正規化 ({dt:.1f}s)")

    return results


def step4_compare(all_anns: list, ctx_free: dict, counter: Counter):
    """文脈あり vs 文脈なし の一致率を集計"""
    print(f"\n[Step 4] 一致率分析")
    # 各アノテーション (文脈ありIDを持つもの) について、文脈なしIDと比較
    rows = []
    for ann in all_anns:
        if ann["entity_type"] == "cell_line":
            continue
        key = (ann["surface"], ann["entity_type"])
        ctx_id = ann["identifier_ctx"]
        free_id = ctx_free.get(key)
        rows.append({
            "doc_id": ann["doc_id"],
            "surface": ann["surface"],
            "entity_type": ann["entity_type"],
            "id_ctx": ctx_id,
            "id_free": free_id,
            "freq": counter.get(key, 0),
            "len": len(ann["surface"]),
            "agree": ctx_id == free_id and ctx_id is not None,
            "both_none": ctx_id is None and free_id is None,
            "ctx_only": ctx_id is not None and free_id is None,
            "free_only": ctx_id is None and free_id is not None,
            "differ": (ctx_id is not None and free_id is not None and ctx_id != free_id),
        })

    # 全体集計
    total = len(rows)
    agree = sum(r["agree"] for r in rows)
    both_none = sum(r["both_none"] for r in rows)
    ctx_only = sum(r["ctx_only"] for r in rows)
    free_only = sum(r["free_only"] for r in rows)
    differ = sum(r["differ"] for r in rows)

    print(f"\n  全体 ({total} アノテーション):")
    print(f"    完全一致 (両方IDあり同一)  : {agree:5d} ({100*agree/total:.1f}%)")
    print(f"    両方とも未正規化            : {both_none:5d} ({100*both_none/total:.1f}%)")
    print(f"    文脈ありだけ正規化         : {ctx_only:5d} ({100*ctx_only/total:.1f}%)")
    print(f"    文脈なしだけ正規化         : {free_only:5d} ({100*free_only/total:.1f}%)")
    print(f"    IDが異なる (致命的)        : {differ:5d} ({100*differ/total:.1f}%)")

    # エンティティ別
    print(f"\n  エンティティタイプ別:")
    by_type = defaultdict(list)
    for r in rows:
        by_type[r["entity_type"]].append(r)
    print(f"    {'type':10s} {'N':>6s} {'agree%':>8s} {'differ%':>8s} {'ctx_only%':>10s}")
    for etype, rs in by_type.items():
        n = len(rs)
        a = sum(r["agree"] for r in rs)
        d = sum(r["differ"] for r in rs)
        c = sum(r["ctx_only"] for r in rs)
        print(f"    {etype:10s} {n:>6d} {100*a/n:>7.1f}% {100*d/n:>7.1f}% {100*c/n:>9.1f}%")

    # 表層形長さ別
    print(f"\n  表層形長さ別 (文字数):")
    print(f"    {'range':12s} {'N':>6s} {'agree%':>8s} {'differ%':>8s}")
    buckets = [(1, 3), (4, 6), (7, 10), (11, 20), (21, 999)]
    for lo, hi in buckets:
        rs = [r for r in rows if lo <= r["len"] <= hi]
        if not rs:
            continue
        n = len(rs)
        a = sum(r["agree"] for r in rs)
        d = sum(r["differ"] for r in rs)
        label = f"{lo}-{hi}" if hi < 999 else f"{lo}+"
        print(f"    {label:12s} {n:>6d} {100*a/n:>7.1f}% {100*d/n:>7.1f}%")

    # 頻度別
    print(f"\n  頻度別 (この検証内での出現回数):")
    print(f"    {'freq':12s} {'N':>6s} {'agree%':>8s} {'differ%':>8s}")
    for lo, hi in [(1, 1), (2, 5), (6, 20), (21, 999)]:
        rs = [r for r in rows if lo <= r["freq"] <= hi]
        if not rs:
            continue
        n = len(rs)
        a = sum(r["agree"] for r in rs)
        d = sum(r["differ"] for r in rs)
        label = f"{lo}-{hi}" if hi < 999 else f"{lo}+"
        print(f"    {label:12s} {n:>6d} {100*a/n:>7.1f}% {100*d/n:>7.1f}%")

    # 不一致事例 (differ) の質的分析
    print(f"\n  不一致事例 (最大30件):")
    differ_rows = [r for r in rows if r["differ"]]
    if not differ_rows:
        print(f"    (なし)")
    else:
        # surface ごとに集約
        differ_by_surf = defaultdict(list)
        for r in differ_rows:
            differ_by_surf[(r["surface"], r["entity_type"])].append(r)
        sorted_keys = sorted(differ_by_surf.keys(), key=lambda k: -len(differ_by_surf[k]))
        shown = 0
        for key in sorted_keys:
            if shown >= 30:
                break
            surf, etype = key
            rs = differ_by_surf[key]
            r0 = rs[0]
            print(f"    [{etype:9s}] {surf!r:30s} ctx={r0['id_ctx']!r} "
                  f"vs free={r0['id_free']!r}  (x{len(rs)})")
            shown += 1

    return rows


def export_parquet(rows: list, path: Path):
    """結果を Parquet に書き出し (後段の分析用)"""
    try:
        import pandas as pd
    except ImportError:
        print("\n  pandas未インストール、Parquet出力スキップ")
        return
    df = pd.DataFrame(rows)
    df.to_parquet(path, index=False)
    print(f"\n  Parquet出力: {path} ({len(df)} 行)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda:2")
    ap.add_argument("--xml-dir", type=Path, required=True)
    ap.add_argument("--max-docs", type=int, default=10)
    ap.add_argument("--output", type=Path, default=Path("context_impact.parquet"))
    args = ap.parse_args()

    print("=" * 78)
    print("Phase 3a: linker文脈影響の検証ベンチ")
    print(f"  device: {args.device}, max_docs: {args.max_docs}")
    print("=" * 78)

    # 1. 前処理 + 文脈ありlinker
    docs = collect_docs(args.xml_dir, args.max_docs)
    print(f"\n[Setup] 前処理: {len(docs)} 本収集")
    for d in docs:
        n = sum(len(p.text) for p in d.passages)
        print(f"  {d.id}: {n:,} chars")
    all_anns, annotator = step1_context_aware_linking(docs, args.device)

    # 2. ユニーク表層形
    counter = step2_unique_surfaces(all_anns)

    # 3. 文脈なしlinker
    ctx_free = step3_context_free_linking(counter, annotator)

    # 4. 比較
    rows = step4_compare(all_anns, ctx_free, counter)

    # 5. Parquet出力
    export_parquet(rows, args.output)

    # 最終判定
    print("\n" + "=" * 78)
    print("判定指針:")
    print("  - 全体一致率 >95% かつ differ% <2% なら二段構え設計は本番投入可")
    print("  - エンティティ別で gene/disease/chemical/species の中に")
    print("    一致率が極端に低いものがあれば、そのタイプだけ文脈ありで処理する")
    print("  - 表層形が短い (1-3文字) ものほど曖昧性が高く不一致が出やすい")
    print("    → 短い表層形だけ文脈あり処理にする折衷案もあり")
    print("=" * 78)


if __name__ == "__main__":
    main()
