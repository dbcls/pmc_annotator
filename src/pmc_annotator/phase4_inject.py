"""
Phase 4: Phase 1 のアノテーション shard に Phase 3b のID辞書を注入.

設計:
  既存の identifier 列を温存 (NULL のときだけ辞書から書き込む)。
  これで Phase 1 で短表層形 linker から得た ID は失われない。

入力:
  - Phase 1 のディレクトリ (annotations/shard_*.parquet)
  - Phase 3b の identifier_dict.parquet

出力:
  - 統合 annotations.parquet (or shard 別)
  - 統計サマリ

使い方:
  python -m pmc_annotator.phase4_inject \
      --phase1-dir ./data/phase1 \
      --identifier-dict ./data/phase3b/identifier_dict.parquet \
      --output-dir ./data/phase4

  # shardごとに分けて出力する場合
  python -m pmc_annotator.phase4_inject ... --per-shard
"""
from __future__ import annotations
import argparse
import json
import time
from pathlib import Path


def inject(phase1_dir: Path,
           identifier_dict: Path,
           output_dir: Path,
           per_shard: bool = False) -> dict:
    import duckdb

    output_dir.mkdir(parents=True, exist_ok=True)
    ann_glob = str(phase1_dir / "annotations" / "shard_*.parquet")

    print(f"[phase4] phase1 annotations: {ann_glob}")
    print(f"[phase4] identifier dict:    {identifier_dict}")

    # JOIN クエリ。COALESCE で既存 identifier を優先。
    join_sql_select = f"""
        SELECT
            a.doc_id, a.pmid, a.shard_id, a.passage_idx, a.passage_type,
            a.ann_id, a.surface, a.entity_type,
            a.offset, a.length, a.score,
            COALESCE(a.identifier, d.identifier)         AS identifier,
            CASE
                WHEN a.identifier IS NOT NULL THEN 'phase1_short_linker'
                WHEN d.identifier IS NOT NULL THEN 'phase3b_contextfree_linker'
                ELSE 'unlinked'
            END AS identifier_source,
            a.source
        FROM '{ann_glob}' a
        LEFT JOIN '{identifier_dict}' d
            ON a.surface = d.surface AND a.entity_type = d.entity_type
    """

    if per_shard:
        # 各 shard を個別に処理
        import glob
        t_start = time.time()
        shards = sorted(Path(phase1_dir / "annotations").glob("shard_*.parquet"))
        shards = [s for s in shards if not s.name.endswith(".done")]
        print(f"[phase4] processing {len(shards)} shards individually")

        per_shard_dir = output_dir / "annotations"
        per_shard_dir.mkdir(parents=True, exist_ok=True)

        for sp in shards:
            shard_id = sp.stem.replace("shard_", "")
            out_path = per_shard_dir / sp.name
            done_marker = out_path.with_suffix(out_path.suffix + ".done")
            if done_marker.exists():
                print(f"[phase4]   {shard_id}: skip (done)")
                continue
            t0 = time.time()
            # shard 別 SELECT
            sql = f"""
                COPY (
                    SELECT
                        a.doc_id, a.pmid, a.shard_id, a.passage_idx, a.passage_type,
                        a.ann_id, a.surface, a.entity_type,
                        a.offset, a.length, a.score,
                        COALESCE(a.identifier, d.identifier) AS identifier,
                        CASE
                            WHEN a.identifier IS NOT NULL THEN 'phase1_short_linker'
                            WHEN d.identifier IS NOT NULL THEN 'phase3b_contextfree_linker'
                            ELSE 'unlinked'
                        END AS identifier_source,
                        a.source
                    FROM '{sp}' a
                    LEFT JOIN '{identifier_dict}' d
                        ON a.surface = d.surface AND a.entity_type = d.entity_type
                ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
            duckdb.query(sql)
            done_marker.touch()
            print(f"[phase4]   {shard_id}: ok ({time.time()-t0:.1f}s)")

        # 統計
        merged_glob = str(per_shard_dir / "shard_*.parquet")
        n_total = duckdb.query(
            f"SELECT COUNT(*) FROM '{merged_glob}'"
        ).fetchone()[0]
        n_with_id = duckdb.query(
            f"SELECT COUNT(*) FROM '{merged_glob}' WHERE identifier IS NOT NULL"
        ).fetchone()[0]
        source_breakdown = duckdb.query(
            f"SELECT identifier_source, COUNT(*) FROM '{merged_glob}' "
            f"GROUP BY identifier_source ORDER BY COUNT(*) DESC"
        ).fetchall()
    else:
        # 1ファイルにまとめて書き出し
        out_path = output_dir / "annotations.parquet"
        sql = f"COPY ({join_sql_select}) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        print(f"[phase4] joining... → {out_path}")
        t_start = time.time()
        duckdb.query(sql)
        print(f"[phase4] join completed in {time.time()-t_start:.1f}s")

        n_total = duckdb.query(
            f"SELECT COUNT(*) FROM '{out_path}'"
        ).fetchone()[0]
        n_with_id = duckdb.query(
            f"SELECT COUNT(*) FROM '{out_path}' WHERE identifier IS NOT NULL"
        ).fetchone()[0]
        source_breakdown = duckdb.query(
            f"SELECT identifier_source, COUNT(*) FROM '{out_path}' "
            f"GROUP BY identifier_source ORDER BY COUNT(*) DESC"
        ).fetchall()

    # サマリ
    print(f"\n[phase4] elapsed: {time.time()-t_start:.1f}s")
    print(f"[phase4] total annotations: {n_total:,}")
    print(f"[phase4] with identifier:   {n_with_id:,} ({100*n_with_id/max(n_total,1):.1f}%)")
    print(f"[phase4] identifier_source breakdown:")
    sb_dict = {}
    for src, cnt in source_breakdown:
        print(f"  {src:35s} {cnt:>12,d}")
        sb_dict[src] = int(cnt)

    summary = {
        "phase1_dir": str(phase1_dir),
        "identifier_dict": str(identifier_dict),
        "n_total": int(n_total),
        "n_with_identifier": int(n_with_id),
        "identifier_source": sb_dict,
    }
    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1-dir", type=Path, required=True)
    ap.add_argument("--identifier-dict", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--per-shard", action="store_true",
                    help="shard ごとに分けて出力する (デフォルトは1ファイル統合)")
    args = ap.parse_args()
    inject(args.phase1_dir, args.identifier_dict, args.output_dir,
           per_shard=args.per_shard)


if __name__ == "__main__":
    main()
