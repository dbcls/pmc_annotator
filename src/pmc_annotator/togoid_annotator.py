"""
TogoIDAnnotator: TogoID準拠の識別子抽出器 (YAML動的ロード版).

togoid_extract_patterns.yaml から SAFE 108種の抽出パターンを読み込み、
本文から TogoID 対応のデータベース識別子を抽出する。
既存の regex_annotator.py (自前12種・文脈フィルタ付き) と並立して使える。
"""
from __future__ import annotations
import re
import yaml
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TogoHit:
    surface: str          # 抽出された生文字列
    entity_type: str      # "accession" (NER系と区別)
    db: str               # TogoID dataset key (例 "ensembl_gene")
    category: str         # TogoID category (例 "Gene")
    identifier: str       # CURIE化したID (例 "ensembl_gene:ENSG00000141510")
    curie: str            # identifiers.org URI付きCURIE (URIがあれば)
    offset: int           # passage内ローカルoffset
    length: int
    confidence: str       # "high"(safe) | "context"(context_required)
    # 実在性検証(手法A)の結果。未検証の間はNoneのまま。
    # existence_integration.verify_hits_existence() が書き込む。
    existence_status: str | None = None   # "verified" | "unverified" | "error" | "skipped_unsafe" | None(未対応DB)
    existence_layer: str | None = None    # "layer1_sparql" | "layer2_alliance" | None


# context_required 層のDBに使う近傍キーワード (DB種別ごと)
_CONTEXT_KEYWORDS = {
    "pdb": ("pdb", "protein data bank", "rcsb", "pdb id", "pdb code",
            "pdb entry", "crystal structure", "deposited"),
}
_CONTEXT_WINDOW = 80


class TogoIDAnnotator:
    def __init__(self, yaml_path=None, use_context_layer: bool = False):
        if yaml_path is None:
            yaml_path = Path(__file__).parent / "data" / "togoid_extract_patterns.yaml"
        self.yaml_path = Path(yaml_path)
        self.use_context_layer = use_context_layer
        self._patterns = []   # (db, category, compiled_regex, uri, confidence, keywords)
        self._load()

    def _load(self):
        with open(self.yaml_path) as f:
            data = yaml.safe_load(f)

        # safe 層
        for db, spec in (data.get("safe") or {}).items():
            pat_str = spec.get("pattern")
            if not pat_str:
                continue
            try:
                rx = re.compile(pat_str)
            except re.error as e:
                print(f"[togoid][warn] skip {db}: regex error {e}")
                continue
            self._patterns.append((
                db, spec.get("category", "?"), rx,
                spec.get("uri"), "high", None,
            ))

        # context_required 層 (オプション)
        if self.use_context_layer:
            for db, spec in (data.get("context_required") or {}).items():
                pat_str = spec.get("pattern")
                if not pat_str:
                    continue
                try:
                    rx = re.compile(pat_str)
                except re.error:
                    continue
                kws = _CONTEXT_KEYWORDS.get(db)
                if not kws:
                    continue
                self._patterns.append((
                    db, spec.get("category", "?"), rx,
                    spec.get("uri"), "context", kws,
                ))

        print(f"[togoid] loaded {len(self._patterns)} patterns "
              f"(context_layer={self.use_context_layer})")

    @staticmethod
    def _has_context(text, start, end, keywords):
        lo = max(0, start - _CONTEXT_WINDOW)
        hi = min(len(text), end + _CONTEXT_WINDOW)
        window = text[lo:hi].lower()
        return any(kw in window for kw in keywords)

    def _make_curie(self, db, surface, uri):
        """抽出IDをCURIE化。TogoID変換は別途 togoid_convertId で行う想定。"""
        local = surface
        ident = f"{db}:{local}"
        curie = (uri + local) if uri else ident
        return ident, curie

    def annotate_text(self, text: str):
        """1つの passage.text から TogoID識別子を抽出。TogoHitのリストを返す。"""
        hits = []
        for db, category, rx, uri, conf, kws in self._patterns:
            for m in rx.finditer(text):
                surface = m.group(0)
                start, end = m.start(), m.end()
                if conf == "context":
                    if not (kws and self._has_context(text, start, end, kws)):
                        continue
                ident, curie = self._make_curie(db, surface, uri)
                hits.append(TogoHit(
                    surface=surface, entity_type="accession", db=db,
                    category=category, identifier=ident, curie=curie,
                    offset=start, length=end - start, confidence=conf,
                ))

        # 重複位置の解決: 長いマッチ優先、同長ならhigh優先
        hits.sort(key=lambda h: (h.offset, -h.length,
                                 0 if h.confidence == "high" else 1))
        resolved = []
        used = []
        for h in hits:
            overlap = any(not (h.offset + h.length <= s or h.offset >= e)
                          for s, e in used)
            if not overlap:
                resolved.append(h)
                used.append((h.offset, h.offset + h.length))
        return resolved


# ===== 単体テスト =====
if __name__ == "__main__":
    import sys
    default = Path(__file__).parent / "data" / "togoid_extract_patterns.yaml"
    yaml_path = sys.argv[1] if len(sys.argv) > 1 else str(default)
    ann = TogoIDAnnotator(yaml_path)

    tests = [
        ("Gene expression data deposited as GSE12345 in GEO.", ["geo_series"]),
        ("The Ensembl gene ENSG00000141510 encodes p53.", ["ensembl_gene"]),
        ("Structure analyzed via Pfam PF00069 and InterPro IPR000719.",
         ["pfam", "interpro"]),
        ("Compound CHEMBL25 and ChEBI CHEBI:15365 were tested.",
         ["chembl_compound", "chebi"]),
        ("Pathway R-HSA-165159 in Reactome; WikiPathways WP1531.",
         ["reactome_pathway", "wikipathways"]),
        ("Variant rs1042522 and ClinVar VCV000012345 analyzed.",
         ["dbsnp", "clinvar"]),
        ("miRNA MI0003587 and RNAcentral URS00000004BF_6239 found.",
         ["mirbase", "rnacentral"]),
        ("Drug DrugBank DB00945; disease MONDO:0005148.",
         ["drugbank", "mondo"]),
        ("We measured 12345 cells at 37C for 24h in 2020.", []),
    ]

    print("\n=== TogoIDAnnotator 単体テスト ===\n")
    all_ok = True
    for text, expected in tests:
        hits = ann.annotate_text(text)
        found = {h.db for h in hits}
        exp = set(expected)
        ok = exp.issubset(found)
        if not expected:
            ok = (len(hits) == 0)
        mark = "✓" if ok else "✗"
        if not ok:
            all_ok = False
        print(f"{mark} {text[:55]}")
        for h in hits:
            print(f"    [{h.category:10s}] {h.db:18s} {h.surface!r} → {h.curie}")
        print()
    print("✓ 全テスト通過" if all_ok else "✗ 失敗あり")
