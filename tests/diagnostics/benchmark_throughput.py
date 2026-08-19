"""
実物 PMC 論文でのスループット計測.

何を計るか:
  - 前処理 (XML パース) の速度
  - HunFlair2 NER のみの速度
  - HunFlair2 + linker (全部) の速度
  - 1本ずつ処理 vs まとめて処理 (本番 shard 投入を想定)
  - linker をエンティティ別に分けた場合の各 linker の所要時間内訳

何を見るか:
  - chars/s (文字スループット) — OA 全体の所要時間見積もりに直結
  - docs/s (論文スループット)
  - NER vs linker の時間配分 → どこがボトルネックか
  - ウォームアップ後の安定値

使い方:
  cd <repo>/pmc_annotator
  # XML ディレクトリ or ファイルを指定
  python tests/benchmark_throughput.py --device cuda:0 --xml-dir /path/to/pmc_xmls --max-docs 5

  # NER のみベンチ (linker を切る)
  python tests/benchmark_throughput.py --device cuda:0 --xml-dir /path/to --no-linkers --max-docs 10

  # 個別の linker 時間内訳を見る
  python tests/benchmark_throughput.py --device cuda:0 --xml-dir /path/to --per-linker-timing
"""
import sys
import time
import argparse
import statistics
from pathlib import Path
from contextlib import contextmanager

_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pmc_annotator.preprocess import JATSParser, iter_jats_files
from pmc_annotator.schema import Document


@contextmanager
def timer(label: str, results: dict):
    """経過時間を辞書に記録するコンテキストマネージャ"""
    t0 = time.time()
    yield
    dt = time.time() - t0
    results.setdefault(label, []).append(dt)


def doc_stats(doc: Document) -> dict:
    """論文の文字数・passage構成統計"""
    n_chars = sum(len(p.text) for p in doc.passages)
    sec_types = [p.infon.get("section_type", "?") for p in doc.passages]
    has_body = any(t in ("introduction", "methods", "results", "discussion", "body")
                   for t in sec_types)
    return {
        "n_chars": n_chars,
        "n_passages": len(doc.passages),
        "has_body": has_body,
        "sec_types": sec_types,
    }


def annotate_with_timing_breakdown(annotator, doc: Document, per_linker_timing: bool) -> dict:
    """1本の論文を流して、NER と各 linker の時間を分けて返す."""
    from pmc_annotator.annotate_hunflair import HunFlairAnnotator  # noqa
    times = {}

    # 文分割
    t0 = time.time()
    all_sentences = []
    sentence_meta = []
    for p_idx, passage in enumerate(doc.passages):
        if not passage.text.strip():
            continue
        sents = annotator._splitter.split(passage.text)
        for s in sents:
            all_sentences.append(s)
            sentence_meta.append((p_idx, s.start_position))
    times["split"] = time.time() - t0

    if not all_sentences:
        return {"times": times, "n_sentences": 0, "n_annotations": 0}

    # NER
    t0 = time.time()
    annotator._tagger.predict(all_sentences,
                              mini_batch_size=annotator.mini_batch_size,
                              verbose=False)
    times["ner"] = time.time() - t0

    # linker (個別計測)
    if per_linker_timing:
        for etype, linker in annotator._linkers.items():
            t0 = time.time()
            try:
                linker.predict(all_sentences)
            except Exception as e:
                print(f"  [warn] linker {etype} failed: {e}")
            times[f"linker_{etype}"] = time.time() - t0
    else:
        t0 = time.time()
        for etype, linker in annotator._linkers.items():
            try:
                linker.predict(all_sentences)
            except Exception:
                pass
        times["linker_all"] = time.time() - t0

    # 結果取り出し (アノテーション数だけカウント)
    n_ann = 0
    for sent in all_sentences:
        n_ann += len(sent.get_spans("ner"))

    return {
        "times": times,
        "n_sentences": len(all_sentences),
        "n_annotations": n_ann,
    }


def collect_docs(xml_dir: Path, max_docs: int) -> list[Document]:
    """前処理を済ませた Document を最大 max_docs 件集める."""
    parser = JATSParser()
    docs = []
    for xml_path in iter_jats_files(xml_dir):
        doc = parser.parse(xml_path)
        if doc is None:
            continue
        # 抄録のみ等、極端に短いものは除外
        n_chars = sum(len(p.text) for p in doc.passages)
        if n_chars < 500:
            continue
        docs.append(doc)
        if len(docs) >= max_docs:
            break
    return docs


def print_summary(results: dict, total_chars_list: list[int], n_anns_list: list[int]):
    """論文1本ごとの時間内訳と統計を表示."""
    print("\n" + "=" * 78)
    print(f"{'metric':<22s} {'mean':>10s} {'median':>10s} {'min':>10s} {'max':>10s}")
    print("-" * 78)
    for label, vals in results.items():
        if not vals:
            continue
        print(f"{label:<22s} {statistics.mean(vals):>10.3f} "
              f"{statistics.median(vals):>10.3f} "
              f"{min(vals):>10.3f} {max(vals):>10.3f}  (秒)")
    print("=" * 78)

    if total_chars_list:
        total_chars = sum(total_chars_list)
        total_anns = sum(n_anns_list)
        total_time = sum(results.get("ner", [])) + sum(
            sum(results.get(k, [])) for k in results if k.startswith("linker"))
        if total_time > 0:
            print(f"  total chars: {total_chars:,}")
            print(f"  total annotations: {total_anns:,}")
            print(f"  total NER+linker time: {total_time:.2f}s")
            print(f"  effective throughput: {total_chars/total_time:.0f} chars/s "
                  f"({total_chars/total_time*60:.0f} chars/min)")


def estimate_oa_runtime(chars_per_sec: float, n_gpus: int = 1,
                        oa_chars: float = 1.8e11):
    """OA 全体 (約 600万報文 × 平均3万文字 ≈ 1.8e11 chars) の所要時間見積もり."""
    seconds = oa_chars / max(chars_per_sec * n_gpus, 1)
    days = seconds / 86400
    print(f"\n  [見積もり] PMC OA 全体 (~{oa_chars:.1e} chars) を "
          f"{chars_per_sec:.0f} chars/s × {n_gpus} GPU で:")
    print(f"  → 約 {days:.1f} 日 ({seconds/3600:.1f} 時間)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--xml-dir", type=Path, required=True,
                    help="PMC JATS XML が入ったディレクトリ (再帰探索)")
    ap.add_argument("--max-docs", type=int, default=5,
                    help="ベンチに使う論文数 (推奨: 5-10)")
    ap.add_argument("--no-linkers", action="store_true",
                    help="linker をスキップ (NER のみ計測)")
    ap.add_argument("--per-linker-timing", action="store_true",
                    help="linker をエンティティ別に時間計測 (内訳が見える)")
    ap.add_argument("--warmup", action="store_true", default=True,
                    help="最初の1本をウォームアップとして計測対象外にする")
    ap.add_argument("--batch-mode", action="store_true",
                    help="全論文を一括投入して、本番 shard 相当のスループットを計測")
    args = ap.parse_args()

    from pmc_annotator.annotate_hunflair import HunFlairAnnotator

    print("=" * 78)
    print(f"PMC スループットベンチマーク")
    print(f"  device: {args.device}, linkers: {'OFF' if args.no_linkers else 'ON'}, "
          f"max_docs: {args.max_docs}")
    print("=" * 78)

    # 1) 前処理
    print(f"\n[1] 前処理: {args.xml_dir} から最大 {args.max_docs} 本収集")
    t0 = time.time()
    docs = collect_docs(args.xml_dir, args.max_docs + (1 if args.warmup else 0))
    parse_time = time.time() - t0
    if not docs:
        print(f"  XMLが見つかりません or 前処理失敗")
        sys.exit(1)
    print(f"  {len(docs)} 本収集, 前処理時間 {parse_time:.2f}s "
          f"({len(docs)/parse_time:.1f} docs/s)")
    for i, d in enumerate(docs):
        st = doc_stats(d)
        print(f"  [{i}] {d.id}: {st['n_chars']:,} chars, {st['n_passages']} passages, "
              f"has_body={st['has_body']}")

    # 2) HunFlair2 ロード
    print(f"\n[2] HunFlair2 ロード")
    t0 = time.time()
    annotator = HunFlairAnnotator(
        device=args.device,
        enable_linkers=not args.no_linkers,
    )
    annotator.load()
    print(f"  ロード時間: {time.time() - t0:.1f}s")

    # 3) ウォームアップ + 計測
    print(f"\n[3] 推論ベンチ")
    if args.warmup and len(docs) > 1:
        print(f"  ウォームアップ (1本目を捨て実行)...")
        _ = annotate_with_timing_breakdown(annotator, docs[0], args.per_linker_timing)
        target_docs = docs[1:]
    else:
        target_docs = docs

    if args.batch_mode:
        # 全 docs をまとめて投入 (本番 shard 相当)
        print(f"  バッチモード: {len(target_docs)} 本を一括投入")
        # docs を1つの大きな文リストにまとめる
        all_sentences = []
        sentence_meta = []
        t0 = time.time()
        for d_idx, doc in enumerate(target_docs):
            for p_idx, passage in enumerate(doc.passages):
                if not passage.text.strip():
                    continue
                sents = annotator._splitter.split(passage.text)
                for s in sents:
                    all_sentences.append(s)
                    sentence_meta.append((d_idx, p_idx))
        t_split = time.time() - t0

        t0 = time.time()
        annotator._tagger.predict(all_sentences,
                                  mini_batch_size=annotator.mini_batch_size,
                                  verbose=False)
        t_ner = time.time() - t0

        t_linkers = {}
        for etype, linker in annotator._linkers.items():
            t0 = time.time()
            try:
                linker.predict(all_sentences)
            except Exception:
                pass
            t_linkers[etype] = time.time() - t0

        total_chars = sum(len(p.text) for d in target_docs for p in d.passages)
        total_anns = sum(len(s.get_spans("ner")) for s in all_sentences)
        total_time = t_split + t_ner + sum(t_linkers.values())

        print(f"\n  [batch] split: {t_split:.2f}s")
        print(f"  [batch] NER:   {t_ner:.2f}s")
        for etype, t in t_linkers.items():
            print(f"  [batch] linker_{etype}: {t:.2f}s")
        print(f"  [batch] TOTAL: {total_time:.2f}s for {len(target_docs)} docs / "
              f"{total_chars:,} chars / {total_anns} annotations")
        print(f"  [batch] throughput: {total_chars/total_time:.0f} chars/s, "
              f"{len(target_docs)/total_time*60:.1f} docs/min")
        estimate_oa_runtime(total_chars/total_time, n_gpus=1)
        estimate_oa_runtime(total_chars/total_time, n_gpus=4)
        estimate_oa_runtime(total_chars/total_time, n_gpus=8)
    else:
        # 1本ずつモード
        results = {}
        chars_list = []
        anns_list = []
        for i, doc in enumerate(target_docs):
            st = doc_stats(doc)
            print(f"  [{i+1}/{len(target_docs)}] {doc.id} ({st['n_chars']:,} chars)...",
                  end=" ", flush=True)
            r = annotate_with_timing_breakdown(annotator, doc, args.per_linker_timing)
            for k, v in r["times"].items():
                results.setdefault(k, []).append(v)
            total_t = sum(r["times"].values())
            print(f"NER={r['times'].get('ner', 0):.2f}s "
                  f"linker={sum(v for k,v in r['times'].items() if k.startswith('linker')):.2f}s "
                  f"total={total_t:.2f}s ({st['n_chars']/max(total_t,0.01):.0f} chars/s, "
                  f"{r['n_annotations']} anns)")
            chars_list.append(st['n_chars'])
            anns_list.append(r['n_annotations'])

        print_summary(results, chars_list, anns_list)

        # OA 全体見積もり (中央値ベース)
        total_chars = sum(chars_list)
        total_time = (sum(results.get("ner", []))
                      + sum(sum(v) for k, v in results.items() if k.startswith("linker"))
                      + sum(results.get("split", [])))
        if total_time > 0:
            est_chars_per_sec = total_chars / total_time
            estimate_oa_runtime(est_chars_per_sec, n_gpus=1)
            estimate_oa_runtime(est_chars_per_sec, n_gpus=4)
            estimate_oa_runtime(est_chars_per_sec, n_gpus=8)


if __name__ == "__main__":
    main()
