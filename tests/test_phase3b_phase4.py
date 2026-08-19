"""
Phase 3b + Phase 4 のロジック確認テスト (GPU不要、Parquet I/O のみ).

linker.predict 部分はモックして、Parquet→Parquet のJOINロジックが
正しく機能するかだけを確認する。
"""
import sys
import json
import tempfile
from pathlib import Path

import pandas as pd
import duckdb


def test_phase4_join():
    """Phase 4 の COALESCE JOIN ロジックを単体で検証"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # Phase 1 風の annotations parquet を作る
        ann_dir = td / "phase1" / "annotations"
        ann_dir.mkdir(parents=True)
        ann_rows = [
            # (doc_id, surface, entity_type, identifier=Phase1での既存値)
            ("PMC1", 0, "T1", "TP53", "gene",    None,         "title", 0, 4, 0.99),
            ("PMC1", 0, "T2", "p53",  "gene",    "NCBIGene:7157", "title", 10, 3, 0.95),  # ← Phase 1で既に埋まっている
            ("PMC1", 0, "T3", "cancer", "disease", None,       "title", 20, 6, 0.99),
            ("PMC2", 0, "T1", "HeLa", "cell_line", None,        "body",  0, 4, 0.99),  # cell_line: 辞書に無い
            ("PMC2", 0, "T2", "unknown_surface", "gene", None,  "body", 10, 15, 0.99),  # 辞書に無い
            ("PMC2", 0, "T3", "TP53", "gene",    None,          "body", 30, 4, 0.99),  # 別文書の重複
        ]
        ann_df = pd.DataFrame(ann_rows, columns=[
            "doc_id", "passage_idx", "ann_id", "surface", "entity_type",
            "identifier", "passage_type", "offset", "length", "score"
        ])
        ann_df["pmid"] = None
        ann_df["shard_id"] = "000000"
        ann_df["source"] = "hunflair2"
        ann_df = ann_df[[
            "doc_id", "pmid", "shard_id", "passage_idx", "passage_type",
            "ann_id", "surface", "entity_type", "offset", "length", "score",
            "identifier", "source",
        ]]
        ann_df.to_parquet(ann_dir / "shard_000000.parquet", index=False)
        print(f"[test] phase1 annotations.parquet: {len(ann_df)} rows")
        print(ann_df.to_string())

        # Phase 3b 風の identifier_dict.parquet を作る
        dict_df = pd.DataFrame([
            ("TP53",   "gene",    "NCBIGene:7157", "ok"),
            ("p53",    "gene",    "NCBIGene:7157", "ok"),  # 同じID
            ("cancer", "disease", "MESH:D009369",  "ok"),
        ], columns=["surface", "entity_type", "identifier", "linker_status"])
        dict_path = td / "phase3b" / "identifier_dict.parquet"
        dict_path.parent.mkdir(parents=True)
        dict_df.to_parquet(dict_path, index=False)
        print(f"\n[test] phase3b dict.parquet: {len(dict_df)} rows")
        print(dict_df.to_string())

        # Phase 4 風 JOIN を実行
        out_dir = td / "phase4"
        out_dir.mkdir()
        ann_glob = str(ann_dir / "shard_*.parquet")
        out_path = out_dir / "annotations.parquet"
        duckdb.query(f"""
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
                FROM '{ann_glob}' a
                LEFT JOIN '{dict_path}' d
                    ON a.surface = d.surface AND a.entity_type = d.entity_type
            ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

        result = duckdb.query(f"SELECT * FROM '{out_path}' ORDER BY doc_id, ann_id").fetchdf()
        print(f"\n[test] phase4 output ({len(result)} rows):")
        print(result[["doc_id", "ann_id", "surface", "entity_type",
                      "identifier", "identifier_source"]].to_string())

        # 検証
        rows_dict = result.set_index(["doc_id", "ann_id"])

        # PMC1/T1: TP53 → Phase 3b 辞書から
        r = rows_dict.loc[("PMC1", "T1")]
        assert r["identifier"] == "NCBIGene:7157", r
        assert r["identifier_source"] == "phase3b_contextfree_linker", r

        # PMC1/T2: p53 → 既に Phase 1 で埋まっていたので温存
        r = rows_dict.loc[("PMC1", "T2")]
        assert r["identifier"] == "NCBIGene:7157", r
        assert r["identifier_source"] == "phase1_short_linker", r

        # PMC1/T3: cancer → Phase 3b 辞書から
        r = rows_dict.loc[("PMC1", "T3")]
        assert r["identifier"] == "MESH:D009369", r
        assert r["identifier_source"] == "phase3b_contextfree_linker", r

        # PMC2/T1: HeLa cell_line → 辞書に無い → unlinked
        r = rows_dict.loc[("PMC2", "T1")]
        assert pd.isna(r["identifier"]), r
        assert r["identifier_source"] == "unlinked", r

        # PMC2/T2: 辞書に無い surface → unlinked
        r = rows_dict.loc[("PMC2", "T2")]
        assert pd.isna(r["identifier"]), r
        assert r["identifier_source"] == "unlinked", r

        # PMC2/T3: TP53 → 別文書でも辞書から付く
        r = rows_dict.loc[("PMC2", "T3")]
        assert r["identifier"] == "NCBIGene:7157", r
        assert r["identifier_source"] == "phase3b_contextfree_linker", r

        print("\n✓ all assertions passed")


def test_phase3b_dict_format():
    """Phase 3b の出力スキーマが Phase 4 と互換であることを確認"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # entity_type 別パーシャル出力を作る
        for etype, rows in [
            ("gene", [("TP53", "NCBIGene:7157", "ok"),
                       ("MDM2", "NCBIGene:4193", "ok"),
                       ("unknown", None, "no_id")]),
            ("disease", [("cancer", "MESH:D009369", "ok")]),
        ]:
            df = pd.DataFrame(rows, columns=["surface", "identifier", "linker_status"])
            df["entity_type"] = etype
            df = df[["surface", "entity_type", "identifier", "linker_status"]]
            df.to_parquet(td / f"identifier_dict_{etype}.parquet", index=False)

        # merge クエリを実行
        glob = str(td / "identifier_dict_*.parquet")
        merged = td / "identifier_dict.parquet"
        duckdb.query(
            f"COPY (SELECT * FROM '{glob}') TO '{merged}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        n = duckdb.query(f"SELECT COUNT(*) FROM '{merged}'").fetchone()[0]
        n_linked = duckdb.query(
            f"SELECT COUNT(*) FROM '{merged}' WHERE identifier IS NOT NULL"
        ).fetchone()[0]
        print(f"[test_phase3b_dict_format] merged: {n} rows, {n_linked} linked")
        assert n == 4
        assert n_linked == 3


if __name__ == "__main__":
    test_phase3b_dict_format()
    print()
    test_phase4_join()
    print("\n✓ test_phase3b_phase4 passed")
