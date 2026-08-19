"""
1,000 shard の最終成果物 (phase4) の中身を多角的に確認する.
"""
import argparse
from pathlib import Path


def main():
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotations", type=Path, required=True)
    ap.add_argument("--doc", default=None, help="特定PMCIDの全アノテーションを表示")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    glob = str(args.annotations / "shard_*.parquet")
    con = duckdb.connect()

    if args.doc:
        print(f"=== {args.doc} の全アノテーション (offset順) ===")
        rows = con.execute(f"""
            SELECT passage_type, "offset", "length", surface, entity_type,
                   identifier, identifier_source
            FROM '{glob}' WHERE doc_id = '{args.doc}'
            ORDER BY "offset"
        """).fetchall()
        if not rows:
            print(f"  {args.doc} が見つかりません")
            return
        for pt, off, length, surf, et, idn, src in rows:
            idstr = idn if idn else "(none)"
            print(f"  [{off:>7d}+{length:<3d}] {et:9s} {surf!r:35s} → {idstr:20s} [{pt}]")
        print(f"\n  計 {len(rows)} アノテーション")
        return

    print("=== 1. 全体統計 ===")
    total = con.execute(f"SELECT COUNT(*) FROM '{glob}'").fetchone()[0]
    with_id = con.execute(f"SELECT COUNT(*) FROM '{glob}' WHERE identifier IS NOT NULL").fetchone()[0]
    n_docs = con.execute(f"SELECT COUNT(DISTINCT doc_id) FROM '{glob}'").fetchone()[0]
    print(f"  総アノテーション: {total:,}")
    print(f"  論文数:           {n_docs:,}")
    print(f"  ID付与:           {with_id:,} ({100*with_id/total:.1f}%)")
    print(f"  1論文平均:        {total/n_docs:.1f} アノテーション")

    print("\n=== 2. identifier_source 内訳 ===")
    for src, cnt in con.execute(f"""
        SELECT identifier_source, COUNT(*) FROM '{glob}'
        GROUP BY identifier_source ORDER BY COUNT(*) DESC
    """).fetchall():
        print(f"  {src:30s} {cnt:>13,d} ({100*cnt/total:.1f}%)")

    print("\n=== 3. entity_type 別 ===")
    print(f"  {'type':10s} {'total':>12s} {'with_id':>12s} {'rate':>7s}")
    for et, tot, wid in con.execute(f"""
        SELECT entity_type, COUNT(*),
               COUNT(*) FILTER (WHERE identifier IS NOT NULL)
        FROM '{glob}' GROUP BY entity_type ORDER BY COUNT(*) DESC
    """).fetchall():
        print(f"  {et:10s} {tot:>12,d} {wid:>12,d} {100*wid/tot:>6.1f}%")

    print("\n=== 4. ID prefix 分布 ===")
    for prefix, cnt in con.execute(f"""
        SELECT
            CASE
                WHEN identifier LIKE 'NCBIGene:%' THEN 'NCBIGene'
                WHEN identifier LIKE 'MESH:%' THEN 'MESH'
                WHEN identifier LIKE 'NCBITaxon:%' THEN 'NCBITaxon'
                WHEN identifier IS NULL THEN '(unlinked)'
                ELSE 'other'
            END AS prefix,
            COUNT(*)
        FROM '{glob}' GROUP BY prefix ORDER BY COUNT(*) DESC
    """).fetchall():
        print(f"  {prefix:15s} {cnt:>13,d} ({100*cnt/total:.1f}%)")

    print(f"\n=== 5. 高頻度アノテーション top {args.top} ===")
    print(f"  {'count':>10s}  {'type':9s} {'surface':35s} {'identifier'}")
    for surf, et, idn, cnt in con.execute(f"""
        SELECT surface, entity_type, ANY_VALUE(identifier), COUNT(*) AS c
        FROM '{glob}'
        GROUP BY surface, entity_type
        ORDER BY c DESC LIMIT {args.top}
    """).fetchall():
        idstr = idn if idn else "(none)"
        print(f"  {cnt:>10,d}  {et:9s} {surf!r:35s} {idstr}")

    print(f"\n=== 6. unlinked 高頻度 top 20 ===")
    for surf, et, cnt in con.execute(f"""
        SELECT surface, entity_type, COUNT(*) AS c
        FROM '{glob}' WHERE identifier IS NULL
        GROUP BY surface, entity_type
        ORDER BY c DESC LIMIT 20
    """).fetchall():
        print(f"  {cnt:>8,d}  {et:9s} {surf!r}")

    print(f"\n=== 7. サンプル論文 ===")
    sample_doc = con.execute(f"""
        SELECT doc_id, COUNT(*) AS c FROM '{glob}'
        GROUP BY doc_id ORDER BY c DESC LIMIT 5
    """).fetchall()
    print(f"  アノテーション数の多い論文:")
    for doc_id, c in sample_doc:
        print(f"    {doc_id}: {c:,} アノテーション")
    print(f"\n  例: python inspect_annotations.py --annotations {args.annotations} --doc {sample_doc[0][0]}")


if __name__ == "__main__":
    main()
