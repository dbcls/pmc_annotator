#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
existence_integration.py
==========================

togoid_annotator.py の TogoIDAnnotator.annotate_text() が返す TogoHit のリストに対して、
unified_verifier.py 経由で実在性検証(手法A)を行い、結果を各TogoHitに書き戻す。

使い方
------
    from togoid_annotator import TogoIDAnnotator
    from existence_integration import verify_hits_existence, check_db_key_alignment

    ann = TogoIDAnnotator()
    all_hits = []
    for passage_text in corpus:
        all_hits.extend(ann.annotate_text(passage_text))

    # まず一度だけ、YAML側のdbキーとexistence_verifier/alliance_verifier側の
    # dbキーが噛み合っているか確認しておく(名前が違うと黙ってスキップされてしまうため)
    check_db_key_alignment(all_hits)

    # 実在性検証を実行し、all_hits の各要素に existence_status / existence_layer を書き込む
    verify_hits_existence(all_hits)

    for h in all_hits:
        if h.existence_status == "unverified":
            pass  # 手法B(LLM文脈判定)へ
        elif h.existence_status == "verified":
            pass  # 高確信、そのまま採用
        elif h.existence_status is None:
            pass  # 層1/2未対応DB。手法Bへ直接回すか、正規表現精度のみで判断
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Set

from unified_verifier import verify_existence, supported_databases

logger = logging.getLogger("existence_integration")


def check_db_key_alignment(hits: List, verbose: bool = True) -> Dict[str, Set[str]]:
    """
    TogoHit群に出現するdbキーの集合と、unified_verifierが対応しているdbキーの集合を
    突き合わせ、不一致を報告する。

    重要: togoid_annotator.py(YAML由来)のdbキー命名と、existence_verifier.py /
    alliance_verifier.py で個別に定義したdbキー命名は、別々に作られたものなので
    自動的には一致が保証されない。特にモデル生物系(mgi/rgd/sgd/wormbase/zfin/
    flybase/xenbase)は、YAML側が "mgi" のような短縮形で、こちらの実装が
    "mgi_gene" のような命名にしている可能性がある。この関数を統合の最初に
    必ず実行し、不一致があれば ALLIANCE_DB_CONFIG / DB_EXISTENCE_CONFIG の
    キー名をYAML側に合わせて修正すること(逆にYAML側を直すのは影響範囲が
    大きいので推奨しない)。

    戻り値: {
        "hits_only": YAML側にはあるがunified_verifier側にないdbキー,
        "verifier_only": unified_verifier側にはあるがYAML側の実データに出現しなかったdbキー,
        "matched": 両方に存在し、突き合わせが取れているdbキー,
    }
    """
    hit_dbs = {h.db for h in hits}
    verifier_dbs = set(supported_databases().keys())

    hits_only = hit_dbs - verifier_dbs
    verifier_only = verifier_dbs - hit_dbs
    matched = hit_dbs & verifier_dbs

    if verbose:
        print(f"[check_db_key_alignment] YAML側に出現したdbキー: {len(hit_dbs)}種類")
        print(f"[check_db_key_alignment] unified_verifier対応dbキー: {len(verifier_dbs)}種類")
        print(f"[check_db_key_alignment] 一致(実在性検証が有効に働く): {sorted(matched)}")
        if hits_only:
            print(
                f"[check_db_key_alignment] ★要確認★ YAML側にあるがunified_verifier未対応"
                f"(層1/2実装のキー名相違 or 単純に層2/3未実装): {sorted(hits_only)}"
            )
        if verifier_only:
            print(
                f"[check_db_key_alignment] 参考: unified_verifier対応だがこのコーパスの"
                f"抽出結果には出現しなかったdbキー: {sorted(verifier_only)}"
            )

    return {"hits_only": hits_only, "verifier_only": verifier_only, "matched": matched}


def verify_hits_existence(hits: List, **kwargs) -> None:
    """
    hits(TogoHitのリスト)を破壊的に更新し、各要素の existence_status / existence_layer に
    実在性検証の結果を書き込む。

    対応外のdbキー(check_db_key_alignmentのhits_onlyに該当するもの)は
    existence_status = None のまま(未処理)にする。エラーにはしない
    (層1/2がまだ実装されていないDBのhitも、抽出パイプライン全体は
    問題なく流れ続けられるようにするため)。

    kwargs は unified_verifier.verify_existence にそのまま渡す
    (例: batch_size, sleep_between_batches, sleep_between_requests)。
    層1・層2で受け付けるキーワードが異なるので、両方にまたがる呼び出しでは
    共通しないキーワードを渡さないよう注意。
    """
    supported = supported_databases()

    # DB別にsurfaceを集約(重複除去)。同じsurfaceが複数箇所に出現しても1回だけ照合する。
    surfaces_by_db: Dict[str, Set[str]] = defaultdict(set)
    for h in hits:
        if h.db in supported:
            surfaces_by_db[h.db].add(h.surface)

    unsupported_dbs = {h.db for h in hits} - set(surfaces_by_db.keys())
    if unsupported_dbs:
        logger.info(
            "層1/2未対応のためスキップしたdbキー: %s (該当hitのexistence_statusはNoneのまま)",
            sorted(unsupported_dbs),
        )

    # DB別にバッチ照合(surfaceをキーにした結果辞書を作る)
    results_by_db: Dict[str, dict] = {}
    for db, surfaces in surfaces_by_db.items():
        logger.info("verify_existence: db=%s, 候補数=%d", db, len(surfaces))
        results_by_db[db] = verify_existence(db, list(surfaces), **kwargs)

    # 各hitに結果を書き戻す
    matched_count = 0
    for h in hits:
        db_results = results_by_db.get(h.db)
        if db_results is not None and h.surface in db_results:
            r = db_results[h.surface]
            h.existence_status = r.status
            h.existence_layer = r.layer
            matched_count += 1
        else:
            h.existence_status = None
            h.existence_layer = None

    logger.info(
        "verify_hits_existence完了: 全%d hit中、%d hitに実在性検証結果を付与",
        len(hits),
        matched_count,
    )


if __name__ == "__main__":
    # 動作確認: togoid_annotator.pyの単体テストと同じ入力で、
    # db_keyの突き合わせが正しく機能するか確認する(ネットワークアクセスなし)
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)

    sys.path.insert(0, str(Path(__file__).parent))
    from togoid_annotator import TogoIDAnnotator

    default_yaml = Path(__file__).parent / "data" / "togoid_extract_patterns.yaml"
    if not default_yaml.exists():
        print(
            f"[動作確認スキップ] {default_yaml} が見つからないため、"
            f"TogoIDAnnotatorの初期化はできません。"
            f"実際のtogoid_extract_patterns.yamlがある環境で実行してください。"
        )
    else:
        ann = TogoIDAnnotator()
        sample_text = (
            "The Ensembl gene ENSG00000141510 encodes p53. "
            "Compound CHEMBL25 was tested. "
            "Pathway R-HSA-165159 in Reactome. "
            "Variant ClinVar VCV000012345 analyzed."
        )
        hits = ann.annotate_text(sample_text)
        print(f"\n抽出hit数: {len(hits)}")

        alignment = check_db_key_alignment(hits)
        print(f"\n一致キー数: {len(alignment['matched'])}")
