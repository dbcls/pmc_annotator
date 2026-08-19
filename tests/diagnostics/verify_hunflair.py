"""
実機 GPU 検証スクリプト.

実行方法 (推奨):
  cd <repo>/src
  python -m pmc_annotator.verify_hunflair --device cuda:0
  # または: PYTHONPATH=<repo>/src python <repo>/tests/verify_hunflair.py --device cuda:0

このスクリプトは src/pmc_annotator/ をパッケージとして import する。
src ディレクトリにいるか、PYTHONPATH に src が入っている必要がある。

目的: HunFlair2 を実機で1論文流し、以下を **目視で** 確認する。
  1. NER タグが取れているか (entity_type の分布)
  2. 正規化ID (link) が取れているか、各エンティティタイプでフォーマットは想定通りか
  3. オフセットが正しいか (doc.full_text()[offset:offset+length] == annotation.text を検証)
  4. pyab3p が効いているか (略語の正規化が効くか)
  5. スループット (1論文あたりの所要時間)

依存:
  pip install flair scispacy
  # (任意・推奨) 略語解決のため:  pip install pyab3p
"""
import sys
import time
import argparse
from pathlib import Path

# パッケージとして実行されない場合のフォールバック:
# このスクリプトを直接実行できるよう src/ を sys.path に追加
_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pmc_annotator.preprocess import JATSParser
from pmc_annotator.annotate_hunflair import HunFlairAnnotator


def check_pyab3p():
    try:
        import pyab3p  # noqa
        print("[check] pyab3p: AVAILABLE (略語解決が有効)")
        return True
    except ImportError:
        print("[check] pyab3p: NOT FOUND (略語の正規化精度が落ちる可能性。"
              "pip install pyab3p を検討)")
        return False


def verify_offsets(doc) -> tuple[int, int]:
    """各 annotation の offset が full_text 上で正しい位置を指すか検証"""
    ft = doc.full_text()
    ok, ng = 0, 0
    for p in doc.passages:
        for a in p.annotations:
            sliced = ft[a.offset:a.offset + a.length]
            if sliced == a.text:
                ok += 1
            else:
                ng += 1
                if ng <= 10:
                    print(f"  [OFFSET MISMATCH] expected={a.text!r} "
                          f"got={sliced!r} (offset={a.offset}, len={a.length})")
    return ok, ng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--xml", type=Path, default=None,
                    help="検証する JATS XML (省略時は tests/sample_pmc.xml)")
    ap.add_argument("--no-linkers", action="store_true",
                    help="linker をスキップして NER のみ (高速確認用)")
    args = ap.parse_args()

    print("=" * 70)
    print("HunFlair2 実機検証")
    print("=" * 70)
    check_pyab3p()

    xml_path = args.xml or (Path(__file__).parent / "sample_pmc.xml")
    print(f"\n[1] 前処理: {xml_path}")
    parser = JATSParser()
    doc = parser.parse(xml_path)
    if doc is None:
        print("  前処理失敗")
        sys.exit(1)
    n_chars = sum(len(p.text) for p in doc.passages)
    print(f"  doc.id={doc.id}, passages={len(doc.passages)}, total_chars={n_chars}")

    print(f"\n[2] HunFlair2 ロード (device={args.device}, "
          f"linkers={'OFF' if args.no_linkers else 'ON'})")
    t0 = time.time()
    annotator = HunFlairAnnotator(
        device=args.device,
        enable_linkers=not args.no_linkers,
    )
    annotator.load()
    print(f"  ロード時間: {time.time() - t0:.1f}s")

    print(f"\n[3] 推論")
    t0 = time.time()
    doc = annotator(doc)
    infer_time = time.time() - t0
    n_ann = sum(len(p.annotations) for p in doc.passages)
    print(f"  推論時間: {infer_time:.2f}s ({n_ann} annotations, "
          f"{n_chars/max(infer_time,0.01):.0f} chars/s)")

    print(f"\n[4] エンティティタイプ分布")
    from collections import Counter
    type_counter = Counter()
    linked_counter = Counter()
    for p in doc.passages:
        for a in p.annotations:
            type_counter[a.entity_type] += 1
            if a.identifiers:
                linked_counter[a.entity_type] += 1
    for etype, count in type_counter.most_common():
        linked = linked_counter.get(etype, 0)
        print(f"  {etype:12s}: {count:4d} 件 (うち正規化済み {linked:4d} 件 "
              f"= {100*linked/count:.0f}%)")

    print(f"\n[5] アノテーション詳細 (最初の25件)")
    shown = 0
    for p in doc.passages:
        for a in p.annotations:
            if shown >= 25:
                break
            ids = ";".join(a.identifiers) if a.identifiers else "(no id)"
            score = f"{a.score:.3f}" if a.score is not None else "?"
            print(f"  [{a.entity_type:9s}] {a.text!r:30s} → {ids:25s} "
                  f"(score={score}, offset={a.offset})")
            shown += 1
        if shown >= 25:
            break

    print(f"\n[6] オフセット整合性チェック")
    ok, ng = verify_offsets(doc)
    print(f"  OK={ok}, NG={ng}")
    if ng == 0:
        print("  ✓ 全アノテーションのオフセットが full_text と一致")
    else:
        print(f"  ✗ {ng} 件のオフセット不一致 — SciSpacySentenceSplitter の "
              f"start_position 仕様を要確認")

    print(f"\n[7] BioC-JSON 出力サンプル (最初の passage)")
    import json
    bioc = doc.to_bioc()
    if bioc["passages"]:
        print(json.dumps(bioc["passages"][0], ensure_ascii=False, indent=2)[:1500])

    print("\n" + "=" * 70)
    print("検証完了。確認ポイント:")
    print("  - [4] で各エンティティの正規化率が極端に低くないか")
    print("  - [5] で gene が NCBIGene:, species が NCBITaxon: 形式になっているか")
    print("  - [6] でオフセット NG=0 か (NG>0 なら splitter のオフセット基準を要調整)")
    print("=" * 70)


if __name__ == "__main__":
    main()
