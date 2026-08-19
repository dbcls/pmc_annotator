"""
Phase 2: Phase 1 で出力された shard Parquet を DuckDB で集計.
"""
from __future__ import annotations
import argparse
import json
import time
from pathlib import Path


def aggregate(phase1_dir: Path, output_dir: Path,
              include_cell_line: bool = False) -> dict:
    import duckdb

    output_dir.mkdir(parents=True, exist_ok=True)
    ann_glob = str(phase1_dir / "annotations" / "shard_*.parquet")
    doc_glob = str(phase1_dir / "documents"   / "shard_*.parquet")

    print(f"[phase2] scanning {ann_glob}")
    con = duckdb.connect(database=":memory:")

    t0 = time.time()
    n_shards = con.execute(
        f"SELECT COUNT(DISTINCT shard_id) FROM '{ann_glob}'"
    ).fetchone()[0]
    n_anns = con.execute(
        f"SELECT COUNT(*) FROM '{ann_glob}'"
    ).fetchone()[0]
    n_docs = con.execute(
        f"SELECT COUNT(*) FROM '{doc_glob}'"
    ).fetchone()[0]
    print(f"[phase2] {n_shards} shards, {n_docs:,} docs, {n_anns:,} annotations "
          f"({time.time()-t0:.1f}s)")

    where_cell_line = "" if include_cell_line else "WHERE entity_type != 'cell_line'"

    print(f"\n[phase2] aggregating unique surfaces...")
    t1 = time.time()
    unique_path = output_dir / "unique_surfaces.parquet"
    con.execute(f"""
        COPY (
            SELECT
                surface,
                entity_type,
                COUNT(*)                            AS frequency,
                COUNT(DISTINCT doc_id)              AS n_docs,
                AVG(score)                          AS avg_score,
                AVG(length)                         AS avg_length,
                MIN(length)                         AS min_length,
                MAX(length)                         AS max_length,
                ANY_VALUE(identifier) FILTER (WHERE identifier IS NOT NULL) AS identifier_known,
                COUNT(*) FILTER (WHERE identifier IS NULL)  AS n_missing_id,
                COUNT(*) FILTER (WHERE identifier IS NOT NULL) AS n_with_id
            FROM '{ann_glob}'
            {where_cell_line}
            GROUP BY surface, entity_type
            ORDER BY frequency DESC
        ) TO '{unique_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n_unique = con.execute(
        f"SELECT COUNT(*) FROM '{unique_path}'"
    ).fetchone()[0]
    print(f"[phase2] unique_surfaces.parquet: {n_unique:,} rows "
          f"({time.time()-t1:.1f}s)")

    print(f"\n[phase2] per-entity_type stats:")
    type_stats = con.execute(f"""
        SELECT
            entity_type,
            COUNT(*) AS n_anns,
            COUNT(*) FILTER (WHERE identifier IS NOT NULL) AS n_with_id,
            COUNT(*) FILTER (WHERE identifier IS NULL) AS n_missing_id
        FROM '{ann_glob}'
        GROUP BY entity_type
        ORDER BY n_anns DESC
    """).fetchall()
    print(f"  {'entity_type':12s} {'n_anns':>12s} {'with_id':>12s} {'missing':>12s}")
    type_stats_dict = {}
    for etype, n_total, n_with, n_miss in type_stats:
        print(f"  {etype:12s} {n_total:>12,d} {n_with:>12,d} {n_miss:>12,d}")
        type_stats_dict[etype] = {
            "n_anns": int(n_total),
            "n_with_id": int(n_with),
            "n_missing_id": int(n_miss),
        }

    print(f"\n[phase2] unique surfaces per entity_type:")
    unique_type_stats = con.execute(f"""
        SELECT
            entity_type,
            COUNT(*) AS n_unique,
            AVG(frequency) AS avg_freq,
            MAX(frequency) AS max_freq
        FROM '{unique_path}'
        GROUP BY entity_type
        ORDER BY n_unique DESC
    """).fetchall()
    print(f"  {'entity_type':12s} {'n_unique':>12s} {'avg_freq':>10s} {'max_freq':>10s}")
    unique_type_stats_dict = {}
    for etype, n_uniq, avg_f, max_f in unique_type_stats:
        print(f"  {etype:12s} {n_uniq:>12,d} {avg_f:>10.1f} {max_f:>10,d}")
        unique_type_stats_dict[etype] = {
            "n_unique": int(n_uniq),
            "avg_freq": float(avg_f),
            "max_freq": int(max_f),
        }

    print(f"\n[phase2] surface length distribution (unique):")
    length_dist = con.execute(f"""
        SELECT
            CASE
                WHEN min_length <= 3 THEN '1-3'
                WHEN min_length <= 6 THEN '4-6'
                WHEN min_length <= 10 THEN '7-10'
                WHEN min_length <= 20 THEN '11-20'
                ELSE '21+'
            END AS length_bucket,
            COUNT(*) AS n_unique,
            SUM(frequency) AS sum_freq
        FROM '{unique_path}'
        GROUP BY 1
        ORDER BY 1
    """).fetchall()
    print(f"  {'bucket':10s} {'n_unique':>10s} {'sum_freq':>14s}")
    for bucket, n_uniq, sum_f in length_dist:
        print(f"  {bucket:10s} {n_uniq:>10,d} {sum_f:>14,d}")

    print(f"\n[phase2] top 20 surfaces by frequency:")
    top20 = con.execute(f"""
        SELECT surface, entity_type, frequency, identifier_known
        FROM '{unique_path}'
        ORDER BY frequency DESC LIMIT 20
    """).fetchall()
    for surf, etype, freq, idk in top20:
        idk_s = f" → {idk}" if idk else ""
        print(f"  {freq:>8,d}x  [{etype:9s}] {surf!r:30s}{idk_s}")

    summary = {
        "phase1_dir": str(phase1_dir),
        "n_shards": int(n_shards),
        "n_docs": int(n_docs),
        "n_annotations": int(n_anns),
        "n_unique_surfaces": int(n_unique),
        "compression_ratio": n_unique / n_anns if n_anns else 0,
        "per_entity_type": type_stats_dict,
        "unique_per_entity_type": unique_type_stats_dict,
    }
    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n[phase2] summary.json written. "
          f"compression: {summary['compression_ratio']*100:.2f}% "
          f"({n_unique:,} unique / {n_anns:,} anns)")

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1-dir", required=True, type=Path)
    ap.add_argument("--output-dir", required=True, type=Path)
    ap.add_argument("--include-cell-line", action="store_true")
    args = ap.parse_args()
    aggregate(args.phase1_dir, args.output_dir, args.include_cell_line)


if __name__ == "__main__":
    main()
