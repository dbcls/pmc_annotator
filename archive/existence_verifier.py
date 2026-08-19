#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
existence_verifier.py
======================

togoid_annotator.py の「手法A: 実在性検証」実装モジュール。

背景
----
正規表現＋ノイズ辞書だけでは過剰除去/過小除去のトレードオフから抜けられないため、
候補IDが実際にDB上に存在するかをRDF Portal SPARQLエンドポイントに照合して判定する。

設計方針（PoCで確認した知見を反映）
------------------------------------
1. TogoIDの変換テーブル(countId/convertId)は実在性検証には使わない。
   TogoIDは「変換先DBとの対応関係が既知のIDだけ」を収載しているため、単に変換関係が
   未収録なだけの正当なIDまで「未確認」と誤判定してしまう(カバレッジバイアス)。
   実在性検証は必ずDB自身の一次情報源(RDF Portal SPARQL / DB自身のネイティブAPI)に問い合わせる。

2. IRI組み立てにTogoIDのprefix(identifiers.org系)をそのまま使ってはいけない。
   RDF Portal側の実データ名前空間はDBごとのMIEファイルのbase_uriを正とする。
   例: Ensemblはidentifiers.org/ensembl/ではなく、rdf.ebi.ac.uk/resource/ensembl/を使う。

3. DBによって照合パターンが異なる(パターン①②③):
   ① IRI直接・base_uri一致        (UniProt, PubChem, NCBI Gene)
   ② IRI直接・base_uri不一致要注意 (Ensembl: RDF Portal固有namespaceが必要)
   ③ プロパティ値一致              (ClinVar: IRIはvariation_idベースだが、文献に登場する
                                     アクセッション番号(VCV...)はcvo:accessionという別プロパティ)

4. 文献表記とDB格納形式に齟齬がある(バージョンサフィックス等)。
   例: ENST00000357654.9 (文献でよく見る形) は DBでは無版の ENST00000357654 でしか
   型トリプルにヒットしない。VCV000856461.4 も同様。
   → 「生の文字列でまず照合 → ダメなら正規化を試して再照合」の2段構えにする。
   正規化で一致した場合は verified_normalized としてログし、除外はしない。

5. 実在確認できなかったIDは「除外」ではなく「未確認」フラグを立てて後段(手法B: LLM文脈判定)
   に渡す。ハード除外はしない。

未対応(層2/層3)
----------------
dbSNP等、RDF Portalに収載されていないDBは本モジュールの対象外。
DB自身のネイティブAPI(dbSNP REST APIなど、TogoIDを経由しない)で別途実装する。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

import requests

logger = logging.getLogger("existence_verifier")


# ---------------------------------------------------------------------------
# 正規化関数群
# ---------------------------------------------------------------------------
# 「適用できなければ None を返す」形にし、候補ごとに複数パターンを順に試せるようにする。

def strip_version_suffix(id_str: str) -> Optional[str]:
    """末尾の .数字 を除去する。例: ENST00000357654.9 -> ENST00000357654
    VCV000856461.4 -> VCV000856461 にも使える(汎用)。
    """
    m = re.match(r"^(.+?)\.\d+$", id_str)
    return m.group(1) if m else None


def strip_isoform_suffix(id_str: str) -> Optional[str]:
    """UniProtアイソフォーム表記を主エントリのaccessionに戻す。
    例: P04637-2 -> P04637
    """
    m = re.match(r"^([A-Z][A-Z0-9]{5,9})-\d+$", id_str)
    return m.group(1) if m else None


def uppercase_normalize(id_str: str) -> Optional[str]:
    """小文字表記を大文字化する。例: 3pfq -> 3PFQ (PDB)
    既に大文字の場合は変化がないのでNoneを返し、正規化不要と判断させる。
    """
    upper = id_str.upper()
    return upper if upper != id_str else None


NORMALIZERS: Dict[str, Callable[[str], Optional[str]]] = {
    "strip_version_suffix": strip_version_suffix,
    "strip_isoform_suffix": strip_isoform_suffix,
    "uppercase_normalize": uppercase_normalize,
}


# ---------------------------------------------------------------------------
# ID文字列の安全性チェック(SPARQLインジェクション対策)
# ---------------------------------------------------------------------------
# togoid_annotator.py の正規表現段階を通過済みの候補が来る前提だが、
# 二重の安全網として、IRI化する文字列とリテラルにする文字列を個別に検証する。

_SAFE_IRI_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")


def is_safe_iri_component(id_str: str) -> bool:
    """IRIの一部として埋め込んでよい文字列か(空白・<>・"'・制御文字を含まないか)を確認する。"""
    return bool(_SAFE_IRI_COMPONENT_RE.match(id_str))


def escape_sparql_literal(id_str: str) -> str:
    """SPARQL文字列リテラルとして埋め込む際のエスケープ。"""
    return id_str.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# DB設定
# ---------------------------------------------------------------------------

@dataclass
class DBConfig:
    pattern_type: str  # "iri" | "property" | "xref_property"
    endpoint: str
    prefixes: Dict[str, str]
    type_triple: str = ""  # 例: "a up:Protein" ("xref_property"では未使用)
    base_uri: Optional[str] = None          # pattern_type == "iri" のとき必須
    match_predicate: Optional[str] = None   # pattern_type == "property" のとき必須
    normalize: List[str] = field(default_factory=list)
    graph: Optional[str] = None  # 必要な場合のみ FROM/GRAPH を明示
    literal_typed: bool = False  # True の場合、プロパティ照合のリテラルに ^^xsd:string を付与
                                   # (Reactomeは必須True、Rhea/ClinVar/PubMedはFalse=プレーンリテラル必須)
    # "xref_property" 用: entity --xref_predicate--> xrefノード --xref_db_predicate--> "db値"
    #                                                          --xref_id_predicate--> ?val
    # (Reactomeのように、実体とアクセッション文字列の間にxrefノードが挟まる2段階パターン)
    xref_predicate: Optional[str] = None
    xref_db_predicate: Optional[str] = None
    xref_db_value: Optional[str] = None
    xref_id_predicate: Optional[str] = None


DB_EXISTENCE_CONFIG: Dict[str, DBConfig] = {
    "uniprot": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/sib/sparql",
        base_uri="http://purl.uniprot.org/uniprot/",
        type_triple="a up:Protein",
        prefixes={"up": "http://purl.uniprot.org/core/"},
        normalize=["strip_isoform_suffix"],
    ),
    "pubchem_compound": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/pubchem/sparql",
        base_uri="http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID",
        type_triple="a vocab:Compound",
        prefixes={"vocab": "http://rdf.ncbi.nlm.nih.gov/pubchem/vocabulary#"},
        normalize=[],
    ),
    "ensembl_gene": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/ebi/sparql",
        # 要注意: TogoIDのprefix(identifiers.org/ensembl/)ではなく
        # RDF Portal実データのnamespaceを使う(PoCで確認した罠)。
        base_uri="http://rdf.ebi.ac.uk/resource/ensembl/",
        type_triple="a terms:EnsemblGene",
        prefixes={"terms": "http://rdf.ebi.ac.uk/terms/ensembl/"},
        normalize=["strip_version_suffix"],
    ),
    "ensembl_transcript": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/ebi/sparql",
        base_uri="http://rdf.ebi.ac.uk/resource/ensembl.transcript/",
        type_triple="a terms:EnsemblTranscript",
        prefixes={"terms": "http://rdf.ebi.ac.uk/terms/ensembl/"},
        normalize=["strip_version_suffix"],
    ),
    "ncbigene": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/ncbi/sparql",
        base_uri="http://identifiers.org/ncbigene/",
        type_triple="a insdc:Gene",
        prefixes={"insdc": "http://ddbj.nig.ac.jp/ontologies/nucleotide/"},
        normalize=[],
    ),
    "clinvar": DBConfig(
        pattern_type="property",
        endpoint="https://rdfportal.org/ncbi/sparql",
        type_triple="a cvo:VariationArchiveType",
        match_predicate="cvo:accession",
        prefixes={"cvo": "http://purl.jp/bio/10/clinvar/"},
        normalize=["strip_version_suffix"],
        literal_typed=False,
    ),
    "pdb": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/pdb/sparql",
        base_uri="http://rdf.wwpdb.org/pdb/",
        type_triple="a pdbo:datablock",
        prefixes={"pdbo": "http://rdf.wwpdb.org/schema/pdbx-with-vrptx-v50.owl#"},
        # 要注意: 旧namespace pdbx-v50.owl# は無効(0件になる)。
        normalize=["uppercase_normalize"],  # 文献表記が小文字の場合がある(PDB IDは大文字格納)
        graph="http://rdfportal.org/dataset/pdbj",
    ),
    "reactome_pathway": DBConfig(
        pattern_type="xref_property",
        endpoint="https://rdfportal.org/ebi/sparql",
        prefixes={
            "bp": "http://www.biopax.org/release/biopax-level3.owl#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
        },
        xref_predicate="bp:xref",
        xref_db_predicate="bp:db",
        xref_db_value="Reactome",
        xref_id_predicate="bp:id",
        normalize=["strip_version_suffix"],
        literal_typed=True,  # bp:db/bp:id は ^^xsd:string 必須(Reactomeの罠)
        graph="http://rdf.ebi.ac.uk/dataset/reactome",
    ),
    "reactome_reaction": DBConfig(
        pattern_type="xref_property",
        endpoint="https://rdfportal.org/ebi/sparql",
        prefixes={
            "bp": "http://www.biopax.org/release/biopax-level3.owl#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
        },
        xref_predicate="bp:xref",
        xref_db_predicate="bp:db",
        xref_db_value="Reactome",
        xref_id_predicate="bp:id",
        normalize=["strip_version_suffix"],
        literal_typed=True,
        graph="http://rdf.ebi.ac.uk/dataset/reactome",
    ),
    "chembl_compound": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/ebi/sparql",
        # 要注意: TogoIDのprefix(identifiers.org/chembl.compound/)ではなく実データのnamespaceを使う
        base_uri="http://rdf.ebi.ac.uk/resource/chembl/molecule/",
        type_triple="a cco:SmallMolecule",
        prefixes={"cco": "http://rdf.ebi.ac.uk/terms/chembl#"},
        normalize=[],
        graph="http://rdf.ebi.ac.uk/dataset/chembl",
    ),
    "chembl_target": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/ebi/sparql",
        # chembl_compoundと同じCHEMBLnnnn書式だが、パスが molecule/ ではなく target/
        base_uri="http://rdf.ebi.ac.uk/resource/chembl/target/",
        # 簡略化: cco:SingleProtein のみを型チェック対象にしている(ターゲットの大多数を占める)。
        # ProteinComplex/ProteinFamily型のターゲットは現状この簡易チェックでは拾えない。
        # 必要であれば type_triple を複数呼び出しに分けて対応すること。
        type_triple="a cco:SingleProtein",
        prefixes={"cco": "http://rdf.ebi.ac.uk/terms/chembl#"},
        normalize=[],
        graph="http://rdf.ebi.ac.uk/dataset/chembl",
    ),
    "hgnc": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/primary/sparql",
        base_uri="http://identifiers.org/hgnc/",
        type_triple="a m2r:Gene",
        prefixes={"m2r": "http://med2rdf.org/ontology/med2rdf#"},
        normalize=[],
        graph="http://rdfportal.org/dataset/hgnc",
    ),
    "oma_protein": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/sib/sparql",
        # 要注意: TogoIDのprefix(identifiers.org/oma.protein/)ではなく実データのnamespaceを使う
        base_uri="https://omabrowser.org/oma/info/",
        type_triple="a orth:Protein",
        prefixes={"orth": "http://purl.org/net/orth#"},
        normalize=[],
        graph="http://rdfportal.org/dataset/oma",  # SIB共有endpointのため必須(省略するとUniProt等と同時スキャン)
    ),
    "rhea": DBConfig(
        pattern_type="property",
        endpoint="https://rdfportal.org/sib/sparql",
        type_triple="rdfs:subClassOf rhea:Reaction",
        match_predicate="rhea:accession",
        prefixes={
            "rhea": "http://rdf.rhea-db.org/",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        },
        normalize=[],
        literal_typed=False,  # 要注意: ^^xsd:stringを付けると0件になる(Reactomeと逆)
        graph="http://rdfportal.org/dataset/rhea",
    ),
    "glytoucan": DBConfig(
        pattern_type="iri",
        endpoint="https://ts.glycosmos.org/sparql",
        base_uri="http://rdf.glycoinfo.org/glycan/",
        type_triple="a glycan:Saccharide",
        prefixes={"glycan": "http://purl.jp/bio/12/glyco/glycan#"},
        normalize=[],
        graph="http://rdf.glytoucan.org/core",
    ),
    "glycomotif": DBConfig(
        pattern_type="iri",
        endpoint="https://ts.glycosmos.org/sparql",
        # 要注意: glytoucanと同じID接尾辞だが名前空間が別(rdf.が付かないglycoinfo.org/motif/)
        base_uri="http://glycoinfo.org/motif/",
        type_triple="a gn:Motif",
        prefixes={"gn": "http://glyconavi.org/owl#"},
        normalize=[],
        graph="http://rdf.glycosmos.org/glycans/motif_label",
    ),
    "insdc": DBConfig(
        pattern_type="iri",
        endpoint="https://rdfportal.org/ddbj/sparql",
        base_uri="http://identifiers.org/insdc/",
        type_triple="a nuc:Entry",
        prefixes={"nuc": "http://ddbj.nig.ac.jp/ontologies/nucleotide/"},
        normalize=[],
        graph="http://rdfportal.org/dataset/ddbj",
    ),
    "pubmed": DBConfig(
        pattern_type="property_only",  # 型チェック不要、プロパティ一致のみで実在確認
        endpoint="https://rdfportal.org/ncbi/sparql",
        match_predicate="bibo:pmid",
        prefixes={"bibo": "http://purl.org/ontology/bibo/"},
        normalize=[],
        literal_typed=False,
        graph="http://rdfportal.org/dataset/pubmed",
    ),
}


# ---------------------------------------------------------------------------
# SPARQL 実行
# ---------------------------------------------------------------------------

def _prefix_block(prefixes: Dict[str, str]) -> str:
    return "\n".join(f"PREFIX {p}: <{uri}>" for p, uri in prefixes.items())


def _run_sparql(
    endpoint: str,
    query: str,
    timeout: int = 60,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> List[Dict[str, str]]:
    """SPARQLクエリを実行し、SELECT結果のbindingsをリストで返す。

    POSTを使う(GETだとVALUES句が長い場合にURLが414 Request-URI Too Largeで
    弾かれるため。実機で確認済みの問題)。
    """
    headers = {
        "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"query": query, "format": "json"}

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                endpoint, data=data, headers=headers, timeout=timeout
            )
            resp.raise_for_status()
            result = resp.json()
            return result.get("results", {}).get("bindings", [])
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_exc = exc
            logger.warning(
                "SPARQL query failed (attempt %d/%d) on %s: %s",
                attempt,
                max_retries,
                endpoint,
                exc,
            )
            if attempt < max_retries:
                time.sleep(backoff_seconds * attempt)
    raise RuntimeError(f"SPARQL query failed after {max_retries} attempts") from last_exc


def _build_iri_values_query(cfg: DBConfig, ids: List[str]) -> str:
    values = " ".join(f"<{cfg.base_uri}{i}>" for i in ids)
    graph_open, graph_close = "", ""
    if cfg.graph:
        graph_open, graph_close = f"GRAPH <{cfg.graph}> {{", "}"
    return f"""{_prefix_block(cfg.prefixes)}
SELECT ?x WHERE {{
  {graph_open}
  VALUES ?x {{ {values} }}
  ?x {cfg.type_triple} .
  {graph_close}
}}"""


def _build_property_values_query(cfg: DBConfig, ids: List[str]) -> str:
    suffix = "^^xsd:string" if cfg.literal_typed else ""
    values = " ".join(f'"{escape_sparql_literal(i)}"{suffix}' for i in ids)
    prefixes = dict(cfg.prefixes)
    if cfg.literal_typed:
        prefixes.setdefault("xsd", "http://www.w3.org/2001/XMLSchema#")
    graph_open, graph_close = "", ""
    if cfg.graph:
        graph_open, graph_close = f"GRAPH <{cfg.graph}> {{", "}"
    return f"""{_prefix_block(prefixes)}
SELECT ?val WHERE {{
  {graph_open}
  VALUES ?val {{ {values} }}
  ?x {cfg.type_triple} ;
     {cfg.match_predicate} ?val .
  {graph_close}
}}"""


def _build_property_only_query(cfg: DBConfig, ids: List[str]) -> str:
    """型チェックなしでプロパティ一致のみ確認するクエリ(PubMed等)。"""
    suffix = "^^xsd:string" if cfg.literal_typed else ""
    values = " ".join(f'"{escape_sparql_literal(i)}"{suffix}' for i in ids)
    prefixes = dict(cfg.prefixes)
    if cfg.literal_typed:
        prefixes.setdefault("xsd", "http://www.w3.org/2001/XMLSchema#")
    graph_open, graph_close = "", ""
    if cfg.graph:
        graph_open, graph_close = f"GRAPH <{cfg.graph}> {{", "}"
    return f"""{_prefix_block(prefixes)}
SELECT ?val WHERE {{
  {graph_open}
  VALUES ?val {{ {values} }}
  ?x {cfg.match_predicate} ?val .
  {graph_close}
}}"""


def _build_xref_property_query(cfg: DBConfig, ids: List[str]) -> str:
    """entity --xref_predicate--> xrefノード --db_predicate--> "db値" / --id_predicate--> ?val
    という2段階パターン用のクエリ(Reactome等)。
    """
    suffix = "^^xsd:string" if cfg.literal_typed else ""
    values = " ".join(f'"{escape_sparql_literal(i)}"{suffix}' for i in ids)
    db_suffix = "^^xsd:string" if cfg.literal_typed else ""
    graph_open, graph_close = "", ""
    if cfg.graph:
        graph_open, graph_close = f"GRAPH <{cfg.graph}> {{", "}"
    return f"""{_prefix_block(cfg.prefixes)}
SELECT ?val WHERE {{
  {graph_open}
  VALUES ?val {{ {values} }}
  ?entity {cfg.xref_predicate} ?xref .
  ?xref {cfg.xref_db_predicate} "{cfg.xref_db_value}"{db_suffix} ;
        {cfg.xref_id_predicate} ?val .
  {graph_close}
}}"""


def _sparql_values_check(cfg: DBConfig, ids: List[str]) -> Set[str]:
    """ids のうち実在が確認できたものの集合を返す。
    pattern_type == "iri" の場合、返り値はIRIから逆算した「素のID文字列」の集合。
    pattern_type in ("property", "property_only", "xref_property") の場合、
    返り値はマッチしたリテラル値そのもの。
    """
    if not ids:
        return set()

    if cfg.pattern_type == "iri":
        query = _build_iri_values_query(cfg, ids)
        bindings = _run_sparql(cfg.endpoint, query)
        hits: Set[str] = set()
        for b in bindings:
            iri = b["x"]["value"]
            if iri.startswith(cfg.base_uri):
                hits.add(iri[len(cfg.base_uri):])
        return hits

    elif cfg.pattern_type == "property":
        query = _build_property_values_query(cfg, ids)
        bindings = _run_sparql(cfg.endpoint, query)
        return {b["val"]["value"] for b in bindings}

    elif cfg.pattern_type == "property_only":
        query = _build_property_only_query(cfg, ids)
        bindings = _run_sparql(cfg.endpoint, query)
        return {b["val"]["value"] for b in bindings}

    elif cfg.pattern_type == "xref_property":
        query = _build_xref_property_query(cfg, ids)
        bindings = _run_sparql(cfg.endpoint, query)
        return {b["val"]["value"] for b in bindings}

    else:
        raise ValueError(f"unknown pattern_type: {cfg.pattern_type}")


# ---------------------------------------------------------------------------
# バッチ照合(正規化→照合ロジック)
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    status: str  # "verified_exact" | "verified_normalized" | "unverified" | "skipped_unsafe"
    matched_form: Optional[str] = None
    normalization_applied: Optional[str] = None  # 適用した正規化関数名


def verify_batch(
    db_key: str,
    raw_ids: List[str],
    batch_size: int = 200,
    sleep_between_batches: float = 0.2,
) -> Dict[str, VerificationResult]:
    """
    db_key に対応するDB設定を使って raw_ids の実在性をまとめて確認する。

    戻り値: { raw_id: VerificationResult }
    """
    if db_key not in DB_EXISTENCE_CONFIG:
        raise KeyError(
            f"'{db_key}' は層1(RDF Portal SPARQL)対象外です。"
            f" 層2(DB自身のネイティブAPI)または層3(手法Bへ)で扱ってください。"
        )
    cfg = DB_EXISTENCE_CONFIG[db_key]

    # 安全でない文字列は先に弾く(SPARQLインジェクション対策 / 明らかな誤抽出の除外)
    safe_ids: List[str] = []
    results: Dict[str, VerificationResult] = {}
    checker = is_safe_iri_component if cfg.pattern_type == "iri" else (lambda s: True)
    for raw_id in raw_ids:
        if checker(raw_id):
            safe_ids.append(raw_id)
        else:
            results[raw_id] = VerificationResult(status="skipped_unsafe")

    # dedupe(順序は保持)
    unique_ids = list(dict.fromkeys(safe_ids))

    exact_hits: Set[str] = set()
    for i in range(0, len(unique_ids), batch_size):
        chunk = unique_ids[i : i + batch_size]
        exact_hits |= _sparql_values_check(cfg, chunk)
        if sleep_between_batches:
            time.sleep(sleep_between_batches)

    remaining = [i for i in unique_ids if i not in exact_hits]

    # 正規化を順に試す。normalized_str -> (raw_id, 適用した関数名)
    normalized_map: Dict[str, tuple] = {}
    for raw_id in remaining:
        for norm_fn_name in cfg.normalize:
            normalized = NORMALIZERS[norm_fn_name](raw_id)
            if normalized and normalized != raw_id:
                normalized_map[normalized] = (raw_id, norm_fn_name)
                break  # 1つのIDにつき最初にヒットした正規化ルールのみ試す

    norm_hits: Set[str] = set()
    normalized_keys = list(normalized_map.keys())
    for i in range(0, len(normalized_keys), batch_size):
        chunk = normalized_keys[i : i + batch_size]
        norm_hits |= _sparql_values_check(cfg, chunk)
        if sleep_between_batches:
            time.sleep(sleep_between_batches)

    # 結果を統合
    for raw_id in unique_ids:
        if raw_id in exact_hits:
            results[raw_id] = VerificationResult(
                status="verified_exact", matched_form=raw_id
            )
            continue

        norm_match = next(
            (
                (n, fn_name)
                for n, (r, fn_name) in normalized_map.items()
                if r == raw_id and n in norm_hits
            ),
            None,
        )
        if norm_match:
            n, fn_name = norm_match
            results[raw_id] = VerificationResult(
                status="verified_normalized",
                matched_form=n,
                normalization_applied=fn_name,
            )
        else:
            results[raw_id] = VerificationResult(status="unverified")

    return results


# ---------------------------------------------------------------------------
# 簡易動作確認
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_cases = {
        "uniprot": ["P04637", "P04637-2", "P9MDH5"],
        "pubchem_compound": ["2244", "296", "999999999999"],
        "ensembl_gene": ["ENSG00000012048", "ENSG00000012048.5", "ENSG00000999999999"],
        "ensembl_transcript": ["ENST00000357654", "ENST00000357654.9"],
        "ncbigene": ["7157", "672", "999999999999"],
        "clinvar": ["VCV000856461", "VCV000856461.4", "VCV999999999999"],
    }

    for db, ids in test_cases.items():
        print(f"\n=== {db} ===")
        res = verify_batch(db, ids)
        for raw_id, r in res.items():
            print(f"  {raw_id!r:35s} -> {r.status:20s} matched={r.matched_form}")
