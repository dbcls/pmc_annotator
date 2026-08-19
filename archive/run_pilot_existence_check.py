#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_pilot_existence_check.py
==============================

既存の抽出結果(data/oa/phase_regex_togoid/accession_annotations_togoid/*.parquet)
のうち、少数の文書(PILOT_DOC_LIMIT)に絞って、層1(SPARQL)/層2(Alliance API)の
実在性検証をエンドツーエンドで試験実行する。

対応DB(25種)以外は自動的にスキップされ、existence_status = None のまま残る。

前提: unified_verifier.py, existence_verifier.py, alliance_verifier.py が
同じディレクトリ(or importパス上)にあること。ネットワークアクセスが必要
(RDF Portal SPARQLエンドポイント、Alliance API)。

使い方
------
    python3 run_pilot_existence_check.py

出力
----
    data/oa/phase_regex_togoid/pilot_existence_check.parquet
    (元データ + existence_status / existence_layer / existence_matched_form列)

    加えて、DB別の検証結果サマリーと、unverified(=手法Bへ回す候補)の実例を
    標準出力に表示する。
"""

from __future__ import annotations

import time

import duckdb
import pandas as pd

from unified_verifier import verify_existence, supported_databases

SOURCE_GLOB = "data/oa/phase_regex_togoid/accession_annotations_togoid/*.parquet"
OUTPUT_PATH = "data/oa/phase_regex_togoid/pilot_existence_check.parquet"

# パイロット規模。まず小さめに始めて、問題なければ増やしていく。
PILOT_DOC_LIMIT = 200


def main():
    t0 = time.time()
    con = duckdb.connect()
    supported = supported_databases()
    supported_list_sql = ", ".join(f"'{k}'" for k in supported)

    print(f"[1/5] 対応DB({len(supported)}種)に絞って、先頭{PILOT_DOC_LIMIT}文書分を読み込み中...")
    df = con.execute(f"""
        SELECT doc_id, pmid, db, surface, category, identifier, curie, confidence
        FROM '{SOURCE_GLOB}'
        WHERE db IN ({supported_list_sql})
          AND doc_id IN (
              SELECT DISTINCT doc_id
              FROM '{SOURCE_GLOB}'
              ORDER BY doc_id
              LIMIT {PILOT_DOC_LIMIT}
          )
    """).df()
    print(f"    対象行数: {len(df):,} 行 / db種類数: {df['db'].nunique()}")

    if len(df) == 0:
        print("[終了] 対応DBに該当する行が0件でした。PILOT_DOC_LIMITを増やすか、"
              "対象doc_idの範囲を見直してください。")
        return

    print("\n[2/5] DB別にユニークsurfaceを集約...")
    candidates_by_db = (
        df.groupby("db")["surface"].apply(lambda s: sorted(set(s))).to_dict()
    )
    for db, ids in sorted(candidates_by_db.items(), key=lambda kv: -len(kv[1])):
        print(f"    {db:20s}: ユニーク候補 {len(ids):6d} 件")

    print("\n[3/5] 実在性検証を実行中(層1=SPARQL, 層2=Alliance API)...")
    print("      ※件数の多いDBは時間がかかる場合があります")
    results_by_db = {}
    for db, ids in candidates_by_db.items():
        t_db = time.time()
        results_by_db[db] = verify_existence(db, ids)
        print(f"    {db:20s}: 完了 ({time.time() - t_db:.1f}s)")

    print("\n[4/5] 結果を元データに統合...")

    def lookup(row):
        r = results_by_db.get(row["db"], {}).get(row["surface"])
        if r is None:
            return None, None, None
        return r.status, r.layer, r.matched_form

    statuses, layers, matched_forms = [], [], []
    for _, row in df.iterrows():
        s, l, m = lookup(row)
        statuses.append(s)
        layers.append(l)
        matched_forms.append(m)

    df["existence_status"] = statuses
    df["existence_layer"] = layers
    df["existence_matched_form"] = matched_forms

    df.to_parquet(OUTPUT_PATH, index=False)
    print(f"    書き出し完了: {OUTPUT_PATH}")

    print("\n[5/5] サマリー")
    print("\n=== DB別 existence_status 件数 ===")
    summary = (
        df.groupby(["db", "existence_status"], dropna=False)
        .size()
        .unstack(fill_value=0)
    )
    print(summary.to_string())

    print("\n=== unverified(=手法Bへ回す候補)の実例、DB別 上位10件まで ===")
    unverified = df[df["existence_status"] == "unverified"]
    if len(unverified) == 0:
        print("    (該当なし)")
    else:
        for db, group in unverified.groupby("db"):
            examples = sorted(set(group["surface"]))[:10]
            print(f"    {db}: {examples}")

    print("\n=== verified件数 / unverified件数 / 全体に対する比率 ===")
    n_verified = (df["existence_status"] == "verified").sum()
    n_unverified = (df["existence_status"] == "unverified").sum()
    n_error = (df["existence_status"] == "error").sum()
    n_total = len(df)
    print(f"    verified   : {n_verified:6d} ({n_verified/n_total:.1%})")
    print(f"    unverified : {n_unverified:6d} ({n_unverified/n_total:.1%})")
    print(f"    error      : {n_error:6d} ({n_error/n_total:.1%})")

    print(f"\n総処理時間: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
