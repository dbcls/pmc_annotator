"""
JATS XML → Document (BioC-JSON互換) 変換

PMC OA サブセットの .nxml / .xml を入力に、セクションタイプ付きの passage を生成する。

設計方針:
- lxml で iterparse はせず tree でパース (PMC1論文は通常数MB以下なので問題ない)
- 文字オフセットは文書全体での絶対位置を厳密に保持
- 図表キャプションは別 passage として infon に fig_id / table_id を付ける
- リファレンスリストはデフォルトで除外 (NER対象外)
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional, Iterator
from lxml import etree

from .schema import Document, Passage

# JATS XML 用パーサー: コメント/PI除去 + 壊れXMLのrecover
# huge_tree=True は巨大XML (一部の総説論文等) のテキスト長制限を解除
_JATS_PARSER = etree.XMLParser(
    remove_comments=True,
    remove_pis=True,
    recover=True,
    huge_tree=True,
)


# JATS の sec-type 属性 -> 標準セクションタイプへのマッピング
SEC_TYPE_MAP = {
    "intro": "introduction", "introduction": "introduction",
    "materials|methods": "methods", "methods": "methods",
    "materials-and-methods": "methods", "subjects": "methods",
    "results": "results", "results-and-discussion": "results",
    "discussion": "discussion", "conclusions": "conclusion",
    "conclusion": "conclusion",
    "background": "background",
    "case": "case", "cases": "case",
    "supplementary-material": "supplementary",
    "abstract": "abstract",
}


def _text_content(elem) -> str:
    """要素配下のテキストを itertext で結合 (タグは除去、空白を正規化)"""
    if elem is None:
        return ""
    # コメント・処理命令ノードは itertext() が使えないのでスキップ.
    # lxml では通常要素の .tag は str、コメント/PI は関数オブジェクトになる。
    if not isinstance(elem.tag, str):
        return ""
    parts = []
    for t in elem.itertext():
        if t:
            parts.append(t)
    text = "".join(parts)
    # 過剰な空白の畳み込み (改行はスペースに、複数空白を1つに)
    text = " ".join(text.split())
    return text


def _classify_section(sec_elem) -> str:
    """<sec> 要素のセクションタイプを推定"""
    sec_type = sec_elem.get("sec-type", "").lower()
    if sec_type in SEC_TYPE_MAP:
        return SEC_TYPE_MAP[sec_type]
    # sec-type が無ければ <title> から推定
    title_elem = sec_elem.find("./title")
    if title_elem is not None:
        title = _text_content(title_elem).lower()
        for keyword, mapped in [
            ("introduc", "introduction"),
            ("method", "methods"),
            ("material", "methods"),
            ("result", "results"),
            ("discuss", "discussion"),
            ("conclu", "conclusion"),
            ("background", "background"),
        ]:
            if keyword in title:
                return mapped
    return "body"


class JATSParser:
    """1ファイル分のJATS XMLをDocumentに変換するパーサ"""

    def __init__(self,
                 include_captions: bool = True,
                 include_tables_text: bool = False,
                 include_refs: bool = False):
        self.include_captions = include_captions
        self.include_tables_text = include_tables_text
        self.include_refs = include_refs

    def parse(self, xml_path: Path) -> Optional[Document]:
        try:
            tree = etree.parse(str(xml_path), _JATS_PARSER)
        except etree.XMLSyntaxError as e:
            print(f"[parse error] {xml_path}: {e}")
            return None

        root = tree.getroot()

        # メタ情報の取得
        pmcid = self._get_article_id(root, "pmc") or xml_path.stem
        pmid = self._get_article_id(root, "pmid")
        doi = self._get_article_id(root, "doi")

        # タイトル
        title_elem = root.find(".//article-meta/title-group/article-title")
        title = _text_content(title_elem) if title_elem is not None else ""

        doc = Document(
            id=f"PMC{pmcid}" if pmcid.isdigit() else pmcid,
            pmid=pmid,
            doi=doi,
            infon={
                "journal": _text_content(root.find(".//journal-title")),
                "year": _text_content(root.find(".//pub-date/year")),
            },
        )

        cursor = 0  # 文書全体オフセット

        # 1. Title
        if title:
            doc.passages.append(Passage(
                offset=cursor, text=title,
                infon={"section_type": "title"}
            ))
            cursor += len(title) + 1  # +1 は passage 間のセパレータ相当

        # 2. Abstract
        for abst in root.findall(".//abstract"):
            abst_text = self._extract_abstract(abst)
            if abst_text:
                doc.passages.append(Passage(
                    offset=cursor, text=abst_text,
                    infon={"section_type": "abstract"}
                ))
                cursor += len(abst_text) + 1

        # 3. Body sections
        body = root.find(".//body")
        if body is not None:
            for sec in body.findall(".//sec"):
                # 入れ子の sec を二重に拾わないよう、トップレベルのみ処理
                # (より厳密にやるなら ancestor::sec が無いものに限定)
                if sec.getparent().tag == "sec":
                    continue
                section_type = _classify_section(sec)
                sec_text = self._extract_section_text(sec)
                if sec_text:
                    doc.passages.append(Passage(
                        offset=cursor, text=sec_text,
                        infon={"section_type": section_type}
                    ))
                    cursor += len(sec_text) + 1

            # 4. Figure captions
            if self.include_captions:
                for fig in body.findall(".//fig"):
                    cap = fig.find(".//caption")
                    if cap is not None:
                        cap_text = _text_content(cap)
                        if cap_text:
                            doc.passages.append(Passage(
                                offset=cursor, text=cap_text,
                                infon={
                                    "section_type": "fig_caption",
                                    "fig_id": fig.get("id", "") or "",
                                }
                            ))
                            cursor += len(cap_text) + 1

                for tbl in body.findall(".//table-wrap"):
                    cap = tbl.find(".//caption")
                    if cap is not None:
                        cap_text = _text_content(cap)
                        if cap_text:
                            doc.passages.append(Passage(
                                offset=cursor, text=cap_text,
                                infon={
                                    "section_type": "table_caption",
                                    "table_id": tbl.get("id", "") or "",
                                }
                            ))
                            cursor += len(cap_text) + 1

        return doc

    @staticmethod
    def _get_article_id(root, pub_id_type: str) -> Optional[str]:
        for aid in root.findall(".//article-id"):
            if aid.get("pub-id-type") == pub_id_type:
                return (aid.text or "").strip()
        return None

    @staticmethod
    def _extract_abstract(abst_elem) -> str:
        # 構造化抄録 (sec ごとに section-title + text) も平坦化
        parts = []
        for child in abst_elem:
            if child.tag == "title":
                continue  # "Abstract" 等は除外
            parts.append(_text_content(child))
        return " ".join(p for p in parts if p)

    @staticmethod
    def _extract_section_text(sec_elem) -> str:
        # title を除いた本文を結合
        parts = []
        for child in sec_elem:
            if child.tag == "title":
                continue
            if child.tag in ("fig", "table-wrap", "supplementary-material"):
                continue  # 別 passage で処理
            parts.append(_text_content(child))
        return " ".join(p for p in parts if p)


def iter_jats_files(input_dir: Path, pattern: str = "*.xml") -> Iterator[Path]:
    """入力ディレクトリ配下の JATS XML ファイルを再帰的に列挙"""
    yield from input_dir.rglob(pattern)
    yield from input_dir.rglob("*.nxml")
