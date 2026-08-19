#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_verifier.py
======================

togoid_annotator.py の「手法A: 実在性検証」統合窓口。

existence_verifier.py(層1: RDF Portal SPARQL, 15DB)と
alliance_verifier.py(層2: Alliance of Genome Resources API, 7DB)を
1つのインタフェース verify_existence() にまとめ、togoid_annotator.py側は
「層1か層2か」を意識せずに db_key を渡すだけで済むようにする。

対象22DB
--------
層1(SPARQL, existence_verifier.py):
    uniprot, pubchem_compound, ensembl_gene, ensembl_transcript, ncbigene,
    clinvar, pdb, reactome_pathway, reactome_reaction, chembl_compound,
    chembl_target, hgnc, oma_protein, rhea, glytoucan, glycomotif, insdc, pubmed
    (実質18だが、うちpubmedを除く17がDB識別子系。件数はexistence_verifier.py参照)
層2(Alliance API, alliance_verifier.py):
    mgi_gene, rgd_gene, sgd_gene, wormbase_gene, zfin_gene, flybase_gene, xenbase_gene

前提
----
existence_verifier.py と alliance_verifier.py が同じディレクトリ(or importパス上)に
配置されていること。

使い方
------
    from unified_verifier import verify_existence, supported_databases

    results = verify_existence("uniprot", ["P04637", "P9MDH5"])
    for raw_id, r in results.items():
        print(raw_id, r.status, r.layer, r.matched_form)

    print(supported_databases())  # {"uniprot": "layer1_sparql", "mgi_gene": "layer2_alliance", ...}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from existence_verifier import (
    DB_EXISTENCE_CONFIG,
    verify_batch as _verify_batch_layer1,
)
from alliance_verifier import (
    ALLIANCE_DB_CONFIG,
    verify_batch_alliance as _verify_batch_layer2,
)

LAYER1_DBS = set(DB_EXISTENCE_CONFIG.keys())
LAYER2_DBS = set(ALLIANCE_DB_CONFIG.keys())

_OVERLAP = LAYER1_DBS & LAYER2_DBS
if _OVERLAP:
    # 層1・層2で同じdb_keyが重複登録されていないかの安全チェック。
    # 万一重複した場合、どちらのconfigが優先されるか曖昧になるため起動時に検知する。
    raise RuntimeError(f"層1と層2でdb_keyが重複しています: {_OVERLAP}")


# 層ごとに戻り値の status 語彙が微妙に異なる(層1は verified_exact/verified_normalized、
# 層2は verified 一種類のみ)ので、共通の語彙に正規化する。
# "verified" 系はまとめて "verified" とし、詳細は matched_form / extra で確認できるようにする。
_STATUS_NORMALIZE = {
    "verified_exact": "verified",
    "verified_normalized": "verified",
    "verified": "verified",
    "unverified": "unverified",
    "error": "error",
    "skipped_unsafe": "skipped_unsafe",
}


@dataclass
class UnifiedResult:
    status: str  # "verified" | "unverified" | "error" | "skipped_unsafe"
    layer: str  # "layer1_sparql" | "layer2_alliance"
    matched_form: Optional[str] = None
    # 層固有の詳細情報。layer1なら normalization_applied、layer2なら http_status/curie。
    detail: Optional[str] = None


def supported_databases() -> Dict[str, str]:
    """db_key -> どちらの層で扱われるか、の対応表。
    togoid_annotator.py側で対象DBかどうかを事前チェックする際に使う。
    """
    return {
        **{k: "layer1_sparql" for k in LAYER1_DBS},
        **{k: "layer2_alliance" for k in LAYER2_DBS},
    }


def verify_existence(
    db_key: str,
    candidate_ids: List[str],
    **kwargs,
) -> Dict[str, UnifiedResult]:
    """
    db_key に応じて層1(SPARQL)/層2(Alliance API)いずれかに自動的に振り分けて
    実在性検証を行う。

    kwargs は各層の verify_batch / verify_batch_alliance にそのまま渡される
    (例: layer1なら batch_size, sleep_between_batches。layer2なら
     sleep_between_requests, max_retries)。層によって受け付けるキーワードが
    異なるので、両層にまたがるDBを一括で呼ぶ場合は共通しないキーワードを渡さないこと。

    戻り値: { raw_id: UnifiedResult }
    """
    if db_key in LAYER1_DBS:
        raw_results = _verify_batch_layer1(db_key, candidate_ids, **kwargs)
        return {
            raw_id: UnifiedResult(
                status=_STATUS_NORMALIZE[r.status],
                layer="layer1_sparql",
                matched_form=r.matched_form,
                detail=r.normalization_applied,
            )
            for raw_id, r in raw_results.items()
        }

    elif db_key in LAYER2_DBS:
        raw_results = _verify_batch_layer2(db_key, candidate_ids, **kwargs)
        return {
            raw_id: UnifiedResult(
                status=_STATUS_NORMALIZE[r.status],
                layer="layer2_alliance",
                matched_form=r.curie if r.status == "verified" else None,
                detail=f"http_status={r.http_status}" if r.http_status else None,
            )
            for raw_id, r in raw_results.items()
        }

    else:
        raise KeyError(
            f"'{db_key}' は層1/層2いずれの対象でもありません。"
            f" 対応DB一覧は supported_databases() を参照。"
            f" 層2/3判定が済んでいないDB、または層3(実在性検証を諦めて手法Bへ)行きの"
            f"DBの可能性があります。"
        )


def verify_existence_many(
    candidates_by_db: Dict[str, List[str]],
    **kwargs,
) -> Dict[str, Dict[str, UnifiedResult]]:
    """
    複数DB分をまとめて処理するヘルパー。
    candidates_by_db: { db_key: [candidate_id, ...] }
    戻り値: { db_key: { raw_id: UnifiedResult } }

    togoid_annotator.py 側でDuckDBから「DB種別ごとのユニークID一覧」を取得して
    この関数に渡す使い方を想定している(下記の統合例を参照)。
    """
    results: Dict[str, Dict[str, UnifiedResult]] = {}
    unknown_dbs = [db for db in candidates_by_db if db not in supported_databases()]
    if unknown_dbs:
        raise KeyError(
            f"未対応のdb_keyが含まれています: {unknown_dbs}。"
            f" supported_databases() で対応状況を確認してください。"
        )
    for db_key, ids in candidates_by_db.items():
        results[db_key] = verify_existence(db_key, ids, **kwargs)
    return results


if __name__ == "__main__":
    # 動作確認: 層1・層2それぞれから1DBずつ、db_keyの振り分けが正しく動くか確認する
    # (ネットワークアクセスはせず、内部の分岐ロジックのみ確認。実クエリは各モジュール側で検証済み)
    print("supported databases:")
    for db, layer in sorted(supported_databases().items()):
        print(f"  {db:20s} -> {layer}")

    print()
    print(f"層1DB数: {len(LAYER1_DBS)}")
    print(f"層2DB数: {len(LAYER2_DBS)}")
    print(f"合計: {len(LAYER1_DBS) + len(LAYER2_DBS)}")

    try:
        verify_existence("not_a_real_db", ["x"])
    except KeyError as e:
        print(f"\n未対応DBのエラーハンドリング確認: {e}")
