"""
Phase 3b: ユニーク表層形 → ID辞書 (linker 一括処理)

入力:  Phase 2 の出力 (unique_surfaces.parquet)
出力:  identifier_dict.parquet
       スキーマ: surface, entity_type, identifier, n_unique_in_input,
                 linker_status (ok/no_id/error)

並列化:
  entity_type 別に独立プロセスで動かす。GPU を 4 枚使えるなら 4 種 (gene/disease/
  chemical/species) を別 GPU に割り当てる。
  CLI には --entity-type フラグがあり、これで担当 entity_type を絞る。

シンプルな使い方:
  # 単一 GPU (全 entity_type をシーケンシャル)
  python -m pmc_annotator.phase3b_link \
      --input ./data/phase2/unique_surfaces.parquet \
      --output ./data/phase3b \
      --device cuda:0

  # 並列 (entity_type 別に別 GPU)
  python -m pmc_annotator.phase3b_link --input ... --output ... \
      --device cuda:0 --entity-type gene &
  python -m pmc_annotator.phase3b_link --input ... --output ... \
      --device cuda:1 --entity-type disease &
  python -m pmc_annotator.phase3b_link --input ... --output ... \
      --device cuda:2 --entity-type chemical &
  python -m pmc_annotator.phase3b_link --input ... --output ... \
      --device cuda:3 --entity-type species &
  wait

  # ランチャー経由
  python -m pmc_annotator.launch_phase3b \
      --input ./data/phase2/unique_surfaces.parquet \
      --output ./data/phase3b \
      --devices cuda:0,cuda:1,cuda:2,cuda:3
"""
from __future__ import annotations
import argparse
import json
import time
from pathlib import Path
from collections import defaultdict


# entity_type別 出力 Parquet (チャンク途中で書き出し → resumeしやすい)
def _output_path(output_dir: Path, etype: str) -> Path:
    return output_dir / f"identifier_dict_{etype}.parquet"


def _done_marker(output_dir: Path, etype: str) -> Path:
    return _output_path(output_dir, etype).with_suffix(".parquet.done")


def link_entity_type(input_path: Path,
                     output_dir: Path,
                     entity_type: str,
                     device: str,
                     chunk_size: int = 5000) -> dict:
    """
    1つの entity_type について、unique_surfaces から該当行を読み込み、
    linker.predict してID辞書を出力する。
    """
    from .annotate_hunflair import (
        HunFlairAnnotator, LINKER_NAMES, INV_ENTITY_TYPE, _normalize_link_id,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    done = _done_marker(output_dir, entity_type)
    if done.exists():
        print(f"[phase3b] [{entity_type}] already done, skip")
        return {"entity_type": entity_type, "status": "skipped"}

    if entity_type not in LINKER_NAMES:
        print(f"[phase3b] [{entity_type}] no linker available, skip")
        return {"entity_type": entity_type, "status": "no_linker"}

    # 入力から該当 entity_type だけ抽出
    import duckdb
    print(f"[phase3b] [{entity_type}] loading surfaces from {input_path}")
    rows = duckdb.query(
        f"SELECT surface FROM '{input_path}' "
        f"WHERE entity_type = '{entity_type}' "
        f"ORDER BY frequency DESC"
    ).fetchall()
    surfaces = [r[0] for r in rows]
    print(f"[phase3b] [{entity_type}] {len(surfaces):,} unique surfaces")
    if not surfaces:
        return {"entity_type": entity_type, "status": "empty"}

    # linker だけロード (NER タガーは不要だが、HunFlairAnnotator の load は
    # tagger も読み込むので、ここでは linker のみ別途ロード)
    print(f"[phase3b] [{entity_type}] loading linker on {device}...")
    t0 = time.time()
    import flair
    import torch
    from flair.models import EntityMentionLinker
    from flair.data import Sentence
    flair.device = torch.device(device)
    linker = EntityMentionLinker.load(LINKER_NAMES[entity_type])
    print(f"[phase3b] [{entity_type}] linker loaded in {time.time()-t0:.1f}s")

    # チャンク処理
    all_results = []  # (surface, identifier_or_None, status)
    n_chunks = (len(surfaces) + chunk_size - 1) // chunk_size
    t_start = time.time()
    for chunk_idx in range(n_chunks):
        chunk_surfaces = surfaces[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size]
        t_chunk = time.time()

        # ダミー Sentence 作成
        sentences = []
        valid_surfaces = []
        invalid = []
        for surf in chunk_surfaces:
            sent = Sentence(surf)
            if len(sent.tokens) == 0:
                invalid.append(surf)
                continue
            span = sent[0:len(sent.tokens)]
            span.add_label("ner", INV_ENTITY_TYPE[entity_type], score=1.0)
            sentences.append(sent)
            valid_surfaces.append(surf)

        for surf in invalid:
            all_results.append((surf, None, "tokenize_fail"))

        if not sentences:
            continue

        # linker predict
        try:
            linker.predict(sentences)
        except Exception as e:
            print(f"[phase3b] [{entity_type}] chunk {chunk_idx} predict failed: {e}")
            for surf in valid_surfaces:
                all_results.append((surf, None, "predict_error"))
            continue

        # 結果抽出
        n_linked_chunk = 0
        for sent, surf in zip(sentences, valid_surfaces):
            spans = sent.get_spans("ner")
            if not spans:
                all_results.append((surf, None, "no_span"))
                continue
            link_label = spans[0].get_label("link")
            if link_label and link_label.value and link_label.value != "O":
                normalized = _normalize_link_id(link_label.value, entity_type)
                all_results.append((surf, normalized, "ok"))
                n_linked_chunk += 1
            else:
                all_results.append((surf, None, "no_id"))

        dt = time.time() - t_chunk
        rate = len(chunk_surfaces) / max(dt, 0.01)
        print(f"[phase3b] [{entity_type}] chunk {chunk_idx+1}/{n_chunks}: "
              f"{len(chunk_surfaces)} surfaces, {n_linked_chunk} linked, "
              f"{dt:.1f}s ({rate:.0f} surf/s)")

    t_total = time.time() - t_start
    print(f"[phase3b] [{entity_type}] DONE: {len(surfaces)} surfaces in {t_total:.1f}s "
          f"({len(surfaces)/t_total:.0f} surf/s)")

    # Parquet 書き出し
    import pandas as pd
    df = pd.DataFrame(all_results, columns=["surface", "identifier", "linker_status"])
    df["entity_type"] = entity_type
    df = df[["surface", "entity_type", "identifier", "linker_status"]]
    out_path = _output_path(output_dir, entity_type)
    df.to_parquet(out_path, index=False, compression="zstd")

    n_ok = (df["linker_status"] == "ok").sum()
    n_no_id = (df["linker_status"] == "no_id").sum()
    print(f"[phase3b] [{entity_type}] wrote {out_path}")
    print(f"[phase3b] [{entity_type}] ok={n_ok}, no_id={n_no_id}, "
          f"other={len(df) - n_ok - n_no_id}")

    done.touch()
    return {
        "entity_type": entity_type,
        "status": "ok",
        "n_unique": len(surfaces),
        "n_linked": int(n_ok),
        "elapsed_s": t_total,
    }


def merge_dicts(output_dir: Path) -> Path:
    """entity_type 別の identifier_dict_*.parquet を統合"""
    import duckdb
    paths = sorted(output_dir.glob("identifier_dict_*.parquet"))
    if not paths:
        print(f"[phase3b] no per-entity dicts found in {output_dir}")
        return None

    merged_path = output_dir / "identifier_dict.parquet"
    glob = str(output_dir / "identifier_dict_*.parquet")
    duckdb.query(
        f"COPY (SELECT * FROM '{glob}') "
        f"TO '{merged_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    n = duckdb.query(f"SELECT COUNT(*) FROM '{merged_path}'").fetchone()[0]
    n_linked = duckdb.query(
        f"SELECT COUNT(*) FROM '{merged_path}' WHERE identifier IS NOT NULL"
    ).fetchone()[0]
    print(f"[phase3b] merged → {merged_path} ({n:,} rows, {n_linked:,} with id)")
    return merged_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True,
                    help="Phase 2 の unique_surfaces.parquet")
    ap.add_argument("--output", type=Path, required=True,
                    help="出力ディレクトリ (identifier_dict_*.parquet が出る)")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--entity-type", default=None,
                    choices=["gene", "disease", "chemical", "species"],
                    help="単一 entity_type だけ処理 (並列実行時に使用)")
    ap.add_argument("--chunk-size", type=int, default=5000,
                    help="linker.predict に一度に渡す表層形数")
    ap.add_argument("--merge", action="store_true",
                    help="全 entity_type の出力を1ファイルに統合")
    args = ap.parse_args()

    if args.merge:
        merge_dicts(args.output)
        return

    if args.entity_type:
        types = [args.entity_type]
    else:
        types = ["disease", "species", "chemical", "gene"]  # 軽い順

    stats = []
    for etype in types:
        s = link_entity_type(args.input, args.output, etype, args.device,
                             chunk_size=args.chunk_size)
        stats.append(s)

    print(f"\n[phase3b] summary:")
    for s in stats:
        print(f"  {s}")


if __name__ == "__main__":
    main()
