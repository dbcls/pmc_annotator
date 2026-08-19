"""
パイプライン本体.

ステージ:
  Stage A: 前処理 (XML -> intermediate BioC-JSON shard)  ※ CPU 並列
  Stage B: アノテーション (intermediate -> output BioC-JSON shard) ※ GPU
  Stage C: (将来) 辞書マッチ + 正規表現 + TogoID 正規化

各 stage は冪等で、shard 単位の .done マーカで再実行スキップ。

使い方:
  # Stage A だけ (前処理のみ)
  python pipeline.py preprocess --input ./data/input --output ./data/intermediate --shard-size 500

  # Stage B (前処理済みに HunFlair2 を適用)
  python pipeline.py annotate --input ./data/intermediate --output ./data/output --device cuda:0

  # 全部
  python pipeline.py all --input ./data/input --intermediate ./data/intermediate --output ./data/output
"""
from __future__ import annotations
import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from multiprocessing import Pool
from typing import Optional

from .schema import Document, Passage, Annotation
from .preprocess import JATSParser, iter_jats_files
from .io_utils import write_shard, shard_done, iter_shard, make_shard_id


# ===================== Stage A: 前処理 =====================

def _parse_one(xml_path_str: str) -> Optional[dict]:
    """multiprocessing 用 (Pool 経由なので関数はトップレベルかつシリアライズ可能)"""
    parser = JATSParser(
        include_captions=True,
        include_tables_text=False,
        include_refs=False,
    )
    doc = parser.parse(Path(xml_path_str))
    if doc is None:
        return None
    return doc.to_bioc()


def stage_preprocess(input_dir: Path, output_dir: Path,
                     shard_size: int = 500, n_workers: int = 8) -> None:
    """JATS XML を shard 単位の BioC-JSON.gz に変換"""
    output_dir.mkdir(parents=True, exist_ok=True)

    xml_files = list(iter_jats_files(input_dir))
    print(f"[stage A] found {len(xml_files)} XML files")
    if not xml_files:
        return

    # shard ごとに分割処理
    for shard_idx, start in enumerate(range(0, len(xml_files), shard_size)):
        chunk = xml_files[start:start + shard_size]
        shard_id = make_shard_id(shard_idx)
        shard_path = output_dir / f"shard_{shard_id}.jsonl.gz"

        if shard_done(shard_path):
            print(f"[stage A] shard {shard_id} already done, skip")
            continue

        t0 = time.time()
        with Pool(n_workers) as pool:
            results = pool.map(_parse_one, [str(p) for p in chunk])

        # 失敗を除外
        bioc_docs = [r for r in results if r is not None]

        # shard 書き出し (内部 dict なので直接 JSONL.gz)
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(shard_path, "wt", encoding="utf-8") as f:
            for d in bioc_docs:
                f.write(json.dumps(d, ensure_ascii=False))
                f.write("\n")
        marker = shard_path.with_suffix(shard_path.suffix + ".done")
        marker.touch()

        dt = time.time() - t0
        print(f"[stage A] shard {shard_id}: {len(bioc_docs)}/{len(chunk)} docs "
              f"in {dt:.1f}s ({len(chunk)/dt:.1f} docs/s)")


# ===================== Stage B: HunFlair2 アノテーション =====================

def _dict_to_document(d: dict) -> Document:
    """BioC-JSON dict -> Document (アノテーション付与のため再構築)"""
    doc = Document(
        id=d["id"],
        pmid=d.get("infons", {}).get("pmid"),
        doi=d.get("infons", {}).get("doi"),
        infon=d.get("infons", {}),
    )
    for p in d.get("passages", []):
        doc.passages.append(Passage(
            offset=p["offset"],
            text=p["text"],
            infon=p.get("infons", {}),
        ))
    return doc


def stage_annotate(input_dir: Path, output_dir: Path, device: str = "cuda:0") -> None:
    """前処理済み shard に HunFlair2 を適用"""
    from .annotate_hunflair import HunFlairAnnotator

    output_dir.mkdir(parents=True, exist_ok=True)
    shards = sorted(input_dir.glob("shard_*.jsonl.gz"))
    print(f"[stage B] found {len(shards)} input shards")

    annotator = HunFlairAnnotator(device=device)
    annotator.load()  # 最初の shard で初期化されるが、明示的に
    print(f"[stage B] HunFlair2 loaded on {device}")

    for shard_path in shards:
        out_path = output_dir / shard_path.name
        if shard_done(out_path):
            print(f"[stage B] {shard_path.name} already done, skip")
            continue

        t0 = time.time()
        out_docs: list[Document] = []
        for raw in iter_shard(shard_path):
            doc = _dict_to_document(raw)
            doc = annotator(doc)
            out_docs.append(doc)

        write_shard(out_docs, out_path)
        dt = time.time() - t0
        n_ann = sum(len(p.annotations) for d in out_docs for p in d.passages)
        print(f"[stage B] {shard_path.name}: {len(out_docs)} docs, "
              f"{n_ann} annotations in {dt:.1f}s")


# ===================== CLI =====================

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pre = sub.add_parser("preprocess", help="Stage A: JATS XML -> BioC-JSON shards")
    p_pre.add_argument("--input", type=Path, required=True)
    p_pre.add_argument("--output", type=Path, required=True)
    p_pre.add_argument("--shard-size", type=int, default=500)
    p_pre.add_argument("--workers", type=int, default=8)

    p_ann = sub.add_parser("annotate", help="Stage B: HunFlair2 annotation")
    p_ann.add_argument("--input", type=Path, required=True)
    p_ann.add_argument("--output", type=Path, required=True)
    p_ann.add_argument("--device", default="cuda:0")

    p_all = sub.add_parser("all", help="A + B end-to-end")
    p_all.add_argument("--input", type=Path, required=True)
    p_all.add_argument("--intermediate", type=Path, required=True)
    p_all.add_argument("--output", type=Path, required=True)
    p_all.add_argument("--shard-size", type=int, default=500)
    p_all.add_argument("--workers", type=int, default=8)
    p_all.add_argument("--device", default="cuda:0")

    args = ap.parse_args()

    if args.cmd == "preprocess":
        stage_preprocess(args.input, args.output, args.shard_size, args.workers)
    elif args.cmd == "annotate":
        stage_annotate(args.input, args.output, args.device)
    elif args.cmd == "all":
        stage_preprocess(args.input, args.intermediate, args.shard_size, args.workers)
        stage_annotate(args.intermediate, args.output, args.device)


if __name__ == "__main__":
    main()
