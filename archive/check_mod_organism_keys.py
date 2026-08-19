#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_mod_organism_keys.py
============================

実コーパスの一部を togoid_annotator.py に通し、モデル生物系DB
(mgi/rgd/sgd/wormbase/zfin/flybase/xenbase)のキー名が
unified_verifier.py側(ALLIANCE_DB_CONFIG)の命名と一致しているかを
ピンポイントで確認するためのスクリプト。

check_db_key_alignment() は全DBまとめてのサマリーしか出さないため、
モデル生物系だけを狙って surface の実例つきで見られるようにしたもの。

使い方
------
1. 下記の "★ここを実データ読み込みに差し替え ★" 部分を、
   お使いのコーパス読み込み方法(DuckDB, パーサ済みJSONL, プレーンテキスト等)
   に合わせて書き換える。
2. python3 check_mod_organism_keys.py を実行。
3. 出力される「モデル生物系と思われるdbキー一覧」と、各キーの surface実例を確認する。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from togoid_annotator import TogoIDAnnotator
from unified_verifier import supported_databases

# unified_verifier側(layer2)が期待しているモデル生物系のキー名
EXPECTED_MOD_ORGANISM_KEYS = {
    "mgi_gene",
    "rgd_gene",
    "sgd_gene",
    "wormbase_gene",
    "zfin_gene",
    "flybase_gene",
    "xenbase_gene",
}

# モデル生物系らしきdbキーを緩く拾うためのキーワード
# (YAML側が別の命名をしていた場合に見つけやすくするため、prefix一致ではなく
#  緩めのキーワード一致にしている)
_MOD_ORGANISM_HINTS = (
    "mgi", "rgd", "sgd", "wormbase", "wb_gene", "zfin", "flybase", "fb_gene", "xenbase",
)


def _looks_like_mod_organism_key(db_key: str) -> bool:
    lower = db_key.lower()
    return any(hint in lower for hint in _MOD_ORGANISM_HINTS)


def main():
    ann = TogoIDAnnotator()

    # ★ここを実データ読み込みに差し替え★
    # 例1: プレーンテキストファイルの束を読む場合
    #     texts = [p.read_text() for p in Path("corpus_sample/").glob("*.txt")]
    # 例2: DuckDBのpassagesテーブルから読む場合
    #     import duckdb
    #     conn = duckdb.connect("pmc.duckdb")
    #     texts = [row[0] for row in conn.execute(
    #         "SELECT text FROM passages LIMIT 2000"
    #     ).fetchall()]
    # 例3: JSONLの場合
    #     import json
    #     texts = []
    #     with open("corpus_sample.jsonl") as f:
    #         for line in f:
    #             texts.append(json.loads(line)["text"])
    texts: list[str] = []  # ← ここに実データを入れる

    if not texts:
        print(
            "[エラー] texts が空です。このスクリプト内の "
            "'★ここを実データ読み込みに差し替え★' の箇所を編集してから実行してください。"
        )
        return

    print(f"[info] {len(texts)}件のpassageを処理します...")

    all_hits = []
    for text in texts:
        all_hits.extend(ann.annotate_text(text))

    print(f"[info] 抽出hit総数: {len(all_hits)}")

    # dbキーごとにsurfaceの実例を集める(最大5件まで)
    surfaces_by_db: dict[str, list[str]] = defaultdict(list)
    for h in all_hits:
        if len(surfaces_by_db[h.db]) < 5:
            surfaces_by_db[h.db].append(h.surface)

    all_db_keys = set(surfaces_by_db.keys())
    mod_organism_like_keys = {k for k in all_db_keys if _looks_like_mod_organism_key(k)}

    print(f"\n=== モデル生物系らしきdbキー: {len(mod_organism_like_keys)}種類 ===")
    if not mod_organism_like_keys:
        print(
            "モデル生物系のIDが1件も抽出されませんでした。"
            "このサンプルにモデル生物(マウス/ラット/酵母/線虫/ゼブラフィッシュ/"
            "ショウジョウバエ/アフリカツメガエル)関連の論文が含まれているか確認してください。"
        )
    for db_key in sorted(mod_organism_like_keys):
        examples = surfaces_by_db[db_key]
        expected = "✓ unified_verifier側と一致" if db_key in EXPECTED_MOD_ORGANISM_KEYS else "✗ unified_verifier側に該当キーなし(要修正)"
        print(f"\n  db_key = {db_key!r}   [{expected}]")
        print(f"    surface実例: {examples}")

    # 期待していたキーのうち、今回のサンプルに1件も出てこなかったものも報告
    missing = EXPECTED_MOD_ORGANISM_KEYS - all_db_keys
    if missing:
        print(
            f"\n[info] このサンプルには出現しなかった(該当生物の論文が"
            f"含まれていない可能性): {sorted(missing)}"
        )

    print("\n=== 全dbキー一覧(参考) ===")
    print(sorted(all_db_keys))


if __name__ == "__main__":
    main()
