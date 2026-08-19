#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alliance_verifier.py
======================

togoid_annotator.py の「手法A: 実在性検証」層2実装モジュール(Alliance of Genome Resources)。

背景
----
existence_verifier.py がカバーする層1(RDF Portal SPARQL)に対して、こちらは
RDF Portalに収載されていないモデル生物系DBを対象にした層2(DB自身のネイティブAPI)。

Alliance of Genome Resources (alliancegenome.org) は以下7つのモデル生物DBを統一APIで
提供しており、TogoIDの該当識別子とちょうど一致する:
    MGI(マウス), RGD(ラット), SGD(酵母), WormBase(線虫),
    ZFIN(ゼブラフィッシュ), FlyBase(ショウジョウバエ), Xenbase(アフリカツメガエル)
これら7DB個別にAPIを実装する代わりに、1つのAllianceエンドポイントで済ませられる。

重要な留意点(未検証)
--------------------
- このサンドボックス環境では alliancegenome.org への疎通確認ができなかった
  (web_fetchはrobots.txt拒否、bash_toolはネットワーク許可リスト外)。
  API自体のURLパターン(/api/gene/{curie})は複数の公式ドキュメント・GitHubリポジトリで
  一貫して確認できたが、実際のHTTPステータスコードの挙動(200/404など)は未検証。
  本番投入前に、やすさんの環境で少数件のテスト呼び出しを行い、
  実在ID/架空IDそれぞれのレスポンスを確認してから使うこと。
- 各DBのCURIE prefix(MGI:, RGD:, SGD:, WB:, ZFIN:, FB:, Xenbase:)は
  Alliance公式ドキュメント・gene report page URLパターンから推定したもの。
  TogoID側のID格納形式(プレフィックス込みか、数字/コードのみか)は要確認。
  以下の DB_CURIE_CONFIG の strip_prefix / add_prefix で調整すること。

使い方(existence_verifier.pyと同様のインタフェース)
-----------------------------------------------------
    from alliance_verifier import verify_batch_alliance
    results = verify_batch_alliance("mgi_gene", ["98341", "MGI:98341", "99999999"])
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("alliance_verifier")

ALLIANCE_API_BASE = "https://www.alliancegenome.org/api/gene/"


@dataclass
class AllianceDBConfig:
    curie_prefix: str  # 例: "MGI:", "WB:", "Xenbase:"
    # TogoIDの候補ID文字列に既にprefixが含まれているかどうかで分岐する。
    # 含まれていなければ curie_prefix を前置してAllianceのCURIE形式を組み立てる。


# TogoIDの識別子タイプ -> Alliance CURIE prefix
# 要確認: TogoID格納形式によっては strip 処理が必要になる可能性がある
ALLIANCE_DB_CONFIG: Dict[str, AllianceDBConfig] = {
    "mgi_gene": AllianceDBConfig(curie_prefix="MGI:"),
    "rgd_gene": AllianceDBConfig(curie_prefix="RGD:"),
    "sgd_gene": AllianceDBConfig(curie_prefix="SGD:"),
    "wormbase_gene": AllianceDBConfig(curie_prefix="WB:"),
    "zfin_gene": AllianceDBConfig(curie_prefix="ZFIN:"),
    "flybase_gene": AllianceDBConfig(curie_prefix="FB:"),
    "xenbase_gene": AllianceDBConfig(curie_prefix="Xenbase:"),
}


def _to_curie(raw_id: str, cfg: AllianceDBConfig) -> str:
    """候補ID文字列をAllianceが期待するCURIE形式に変換する。
    既にprefixが付いている場合(例: "MGI:98341")はそのまま使う。
    """
    if ":" in raw_id:
        return raw_id
    return f"{cfg.curie_prefix}{raw_id}"


@dataclass
class AllianceVerificationResult:
    status: str  # "verified" | "unverified" | "error"
    http_status: Optional[int] = None
    curie: Optional[str] = None


def _check_one(curie: str, timeout: int = 15) -> AllianceVerificationResult:
    """1件のCURIEについてAllianceの/api/gene/{curie}にGETし、存在確認する。
    想定: 200 = 実在、404 = 非実在。それ以外はerror扱いにしてunverified側に倒す
    (誤って除外しないため)。
    ★この200/404の想定自体が未検証。本番投入前に実データで確認すること。★
    """
    url = ALLIANCE_API_BASE + curie
    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("Alliance API request failed for %s: %s", curie, exc)
        return AllianceVerificationResult(status="error", curie=curie)

    if resp.status_code == 200:
        return AllianceVerificationResult(
            status="verified", http_status=200, curie=curie
        )
    elif resp.status_code == 404:
        return AllianceVerificationResult(
            status="unverified", http_status=404, curie=curie
        )
    else:
        logger.warning(
            "Alliance API unexpected status %d for %s", resp.status_code, curie
        )
        return AllianceVerificationResult(
            status="error", http_status=resp.status_code, curie=curie
        )


def verify_batch_alliance(
    db_key: str,
    raw_ids: List[str],
    sleep_between_requests: float = 0.1,
    max_retries: int = 2,
) -> Dict[str, AllianceVerificationResult]:
    """
    db_key: ALLIANCE_DB_CONFIG のキー(mgi_gene, rgd_gene, sgd_gene, wormbase_gene,
            zfin_gene, flybase_gene, xenbase_gene のいずれか)
    raw_ids: 候補ID文字列のリスト

    戻り値: { raw_id: AllianceVerificationResult }

    注意: SPARQLのVALUES一括照合と異なり、Alliance APIは1件ずつのGETリクエストになる。
    候補数が多い場合はレート制限に配慮し、sleep_between_requestsを調整すること。
    大量件数(万単位)を投げる前に、まず数十件で試して応答時間を確認するのが安全。
    """
    if db_key not in ALLIANCE_DB_CONFIG:
        raise KeyError(
            f"'{db_key}' はAlliance対象外です。"
            f" 対象: {list(ALLIANCE_DB_CONFIG.keys())}"
        )
    cfg = ALLIANCE_DB_CONFIG[db_key]

    results: Dict[str, AllianceVerificationResult] = {}
    for raw_id in dict.fromkeys(raw_ids):  # dedupe、順序保持
        curie = _to_curie(raw_id, cfg)

        result = None
        for attempt in range(1, max_retries + 1):
            result = _check_one(curie)
            if result.status != "error":
                break
            if attempt < max_retries:
                time.sleep(0.5 * attempt)

        results[raw_id] = result
        if sleep_between_requests:
            time.sleep(sleep_between_requests)

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 動作確認用サンプル(未検証: 実際にAllianceが返すレスポンスで要確認)
    test_cases = {
        "mgi_gene": ["98341", "99999999999"],  # Snrnp70 / 架空ID
    }
    for db, ids in test_cases.items():
        print(f"\n=== {db} ===")
        res = verify_batch_alliance(db, ids)
        for raw_id, r in res.items():
            print(f"  {raw_id!r:20s} -> {r.status:12s} (http={r.http_status}, curie={r.curie})")
