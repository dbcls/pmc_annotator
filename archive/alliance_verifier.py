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

重要な留意点
------------
- 7DB全て(MGI, WormBase, RGD, SGD, ZFIN, FlyBase, Xenbase)について、
  やすさんによる実機curl検証で同一の挙動を確認済み(2026-07-02):
    - 実在ID: HTTP 200
    - 非実在ID: HTTP 400 (404ではない)、ボディに
      {"statusCode":400,"errors":["No gene found with ID: ..."],"statusCodeName":"Bad Request"}
  404ではなく400が「非実在」を示すという、このAPI固有の設計に対応済み。
  7DB共通の基盤APIであることが実証されたので、この判定ロジックは全DBに安心して適用できる。
- 各DBのCURIE prefix(MGI:, RGD:, SGD:, WB:, ZFIN:, FB:, Xenbase:)も
  実機検証で有効な形式であることを確認済み。
  ただしTogoID側のID格納形式(プレフィックス込みか、数字/コードのみか)との
  整合性は、togoid_annotator.py統合時に別途確認すること。

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
    "rgd": AllianceDBConfig(curie_prefix="RGD:"),
    "sgd": AllianceDBConfig(curie_prefix="SGD:"),
    "wormbase_gene": AllianceDBConfig(curie_prefix="WB:"),
    "zfin_gene": AllianceDBConfig(curie_prefix="ZFIN:"),
    "flybase_gene": AllianceDBConfig(curie_prefix="FB:"),
    "xenbase_gene": AllianceDBConfig(curie_prefix="Xenbase:"),
}


def _to_curie(raw_id: str, cfg: AllianceDBConfig) -> str:
    """候補ID文字列をAllianceが期待するCURIE形式に変換する。

    TogoID側の正規表現は "MGI:12345" と "mgi:12345" のように大文字/小文字
    どちらのprefixも許容している場合がある一方、Alliance APIのCURIEは
    大文字prefix(例: "MGI:")で厳密に固定されている(実機確認済み)。
    そのため、コロンの有無に関わらず、常に cfg.curie_prefix で組み立て直す
    (元のprefixが付いていた場合はローカル部分だけを取り出して使う)。
    """
    local = raw_id.split(":", 1)[1] if ":" in raw_id else raw_id
    return f"{cfg.curie_prefix}{local}"


@dataclass
class AllianceVerificationResult:
    status: str  # "verified" | "unverified" | "error"
    http_status: Optional[int] = None
    curie: Optional[str] = None


def _check_one(curie: str, timeout: int = 15) -> AllianceVerificationResult:
    """1件のCURIEについてAllianceの/api/gene/{curie}にGETし、存在確認する。

    実機検証済み(2026-07-02, やすさんによるcurl確認)の挙動:
      - 実在ID: HTTP 200、gene情報を含むJSONボディ
      - 非実在ID: HTTP 400 (404ではない)、
                  ボディは {"statusCode":400,"errors":["No gene found with ID: ..."],
                            "statusCodeName":"Bad Request"}
    404ではなく400が「非実在」を示す点に注意(このAPI固有の設計)。
    400は本来「不正なリクエスト」全般に使われるステータスなので、
    念のためボディの errors に "No gene found" 文言が含まれる場合のみ
    「非実在」と判定し、それ以外の400(CURIE形式が不正など)は error 扱いにして
    誤って実在/非実在いずれにも倒さないようにする。
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
    elif resp.status_code == 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        errors = body.get("errors", [])
        if any("No gene found" in str(e) for e in errors):
            return AllianceVerificationResult(
                status="unverified", http_status=400, curie=curie
            )
        logger.warning(
            "Alliance API 400 for %s but not a 'not found' response: %s",
            curie,
            errors,
        )
        return AllianceVerificationResult(
            status="error", http_status=400, curie=curie
        )
    elif resp.status_code == 404:
        # ドキュメント上は400が使われるようだが、将来的な仕様変更に備えて
        # 404も非実在として扱えるようにしておく
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
