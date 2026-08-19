"""
BioC-JSON 互換スキーマ + 内部表現

PubTator Central と互換性のある BioC-JSON を主出力としつつ、
内部処理では dataclass で扱う。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------- Passage / Section ----------

# JATS の <sec sec-type="..."> や要素名から推定するセクションタイプ
SECTION_TYPES = {
    "title", "abstract",
    "introduction", "methods", "results", "discussion", "conclusion",
    "background", "case", "supplementary",
    "fig_caption", "table_caption", "table_content",
    "ref_list", "ack",
    "body",  # フォールバック
}


@dataclass
class Passage:
    """BioC-JSON の passage に対応"""
    offset: int                 # 文書全体での文字オフセット
    text: str
    infon: dict = field(default_factory=dict)  # {"section_type": "abstract", ...}
    annotations: list["Annotation"] = field(default_factory=list)

    def to_bioc(self) -> dict:
        return {
            "offset": self.offset,
            "text": self.text,
            "infons": self.infon,
            "annotations": [a.to_bioc() for a in self.annotations],
            "relations": [],
        }


# ---------- Annotation ----------

@dataclass
class Annotation:
    """
    エンティティ単位のアノテーション。
    HunFlair2 / PubDictionaries / regex のいずれの出力もここに統一する。
    """
    id: str                     # passage内ユニークID (例: "T1")
    text: str                   # マッチした表層形
    offset: int                 # 文書全体オフセット (passage.offset + ローカル)
    length: int
    entity_type: str            # gene, disease, chemical, species, cell_line,
                                # protein, pathway, go_term, accession など
    identifiers: list[str] = field(default_factory=list)  # 正規化ID群 (例: ["NCBIGene:7157", "UniProt:P04637"])
    score: Optional[float] = None
    source: str = ""            # "hunflair2" | "pubdict" | "regex:<pattern>"

    def to_bioc(self) -> dict:
        infons = {
            "type": self.entity_type,
            "source": self.source,
        }
        if self.identifiers:
            # PubTator 互換: 複数IDは ; 区切り
            infons["identifier"] = ";".join(self.identifiers)
        if self.score is not None:
            infons["score"] = str(self.score)
        return {
            "id": self.id,
            "infons": infons,
            "text": self.text,
            "locations": [{"offset": self.offset, "length": self.length}],
        }


# ---------- Document ----------

@dataclass
class Document:
    """1論文 = 1 Document"""
    id: str                     # PMCID (例: "PMC1234567")
    pmid: Optional[str] = None
    doi: Optional[str] = None
    infon: dict = field(default_factory=dict)  # journal, year, license など
    passages: list[Passage] = field(default_factory=list)

    def to_bioc(self) -> dict:
        infons = dict(self.infon)
        if self.pmid:
            infons["pmid"] = self.pmid
        if self.doi:
            infons["doi"] = self.doi
        return {
            "id": self.id,
            "infons": infons,
            "passages": [p.to_bioc() for p in self.passages],
            "relations": [],
        }

    def full_text(self) -> str:
        """passage を offset 順に結合した平文を返す (デバッグ用)"""
        parts = []
        cursor = 0
        for p in sorted(self.passages, key=lambda x: x.offset):
            # passage 間のギャップを空白で埋める
            if p.offset > cursor:
                parts.append(" " * (p.offset - cursor))
            parts.append(p.text)
            cursor = p.offset + len(p.text)
        return "".join(parts)


@dataclass
class Collection:
    """BioC-JSON のトップレベル。shard 単位で扱う"""
    source: str = "PMC-OA"
    date: str = ""
    key: str = "pmc_annotator.key"
    documents: list[Document] = field(default_factory=list)

    def to_bioc(self) -> dict:
        return {
            "source": self.source,
            "date": self.date,
            "key": self.key,
            "infons": {},
            "documents": [d.to_bioc() for d in self.documents],
        }
