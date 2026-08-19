"""
RegexAnnotator: 本文から DB accession を正規表現で抽出する.
NER とは独立。passage.text に直接 regex を当てる。GPU不要・高精度。
"""
from __future__ import annotations
import re
from dataclasses import dataclass


_PATTERNS = [
    ("refseq",
     r"\b(?:AC|AP|NC|NG|NM|NP|NR|NT|NW|WP|XM|XP|XR|YP|ZP)_\d+(?:\.\d+)?\b",
     "refseq:", "high", None),
    ("ensembl",
     r"\bENS[A-Z]{0,4}[GTPR]\d{11}(?:\.\d+)?\b",
     "ensembl:", "high", None),
    ("geo",
     r"\bG(?:SE|SM|PL|DS)\d{2,}\b",
     "geo:", "high", None),
    ("sra",
     r"\b(?:[SED]R[RXPSA]|[SED]RZ)\d{4,}\b",
     "insdc.sra:", "high", None),
    ("bioproject",
     r"\bPRJ(?:NA|EB|DB)\d+\b",
     "bioproject:", "high", None),
    ("biosample",
     r"\bSAM[NED][A-Z]?\d+\b",
     "biosample:", "high", None),
    ("chembl",
     r"\bCHEMBL\d+\b",
     "chembl:", "high", None),
    ("dbsnp",
     r"\brs\d{4,}\b",
     "dbsnp:", "high", None),
    ("clinvar",
     r"\b(?:VCV|RCV)\d{9,}(?:\.\d+)?\b",
     "clinvar:", "high", None),
    ("go",
     r"\bGO:\d{7}\b",
     "GO:", "high", None),
    ("ec",
     r"\bEC[ :]?\d+\.\d+\.\d+\.\d+\b",
     "eccode:", "high", None),
    ("doi",
     r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b",
     "doi:", "high", None),
    ("pdb",
     r"\b[0-9][A-Za-z0-9]{3}\b",
     "pdb:", "context", ("pdb", "protein data bank", "rcsb", "pdb id",
                          "pdb code", "pdb entry", "pdb accession")),
    ("uniprot",
     r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b",
     "uniprot:", "context", ("uniprot", "swiss-prot", "swissprot", "trembl",
                             "uniprotkb")),
]

_COMPILED = [(name, re.compile(rx), prefix, conf, kws)
             for name, rx, prefix, conf, kws in _PATTERNS]

_CONTEXT_WINDOW = 80


@dataclass
class RegexHit:
    surface: str
    entity_type: str
    db: str
    identifier: str
    offset: int
    length: int
    confidence: str


def _has_context(text: str, start: int, end: int, keywords) -> bool:
    lo = max(0, start - _CONTEXT_WINDOW)
    hi = min(len(text), end + _CONTEXT_WINDOW)
    window = text[lo:hi].lower()
    return any(kw in window for kw in keywords)


def annotate_text(text: str, require_context_for_ambiguous: bool = True):
    hits = []
    for name, pat, prefix, conf, kws in _COMPILED:
        for m in pat.finditer(text):
            start, end = m.start(), m.end()
            surface = m.group(0)
            if conf == "context" and require_context_for_ambiguous:
                if not (kws and _has_context(text, start, end, kws)):
                    continue
            # PDB ID は必ず英字を含む (全数字 2020/1234 等を除外)
            if name == "pdb" and not re.search(r"[A-Za-z]", surface):
                continue
            id_body = surface
            if name == "ec":
                id_body = re.sub(r"^EC[ :]?", "", surface)
            elif name == "go":
                id_body = surface.replace("GO:", "")
            elif name == "pdb":
                id_body = surface.upper()
            identifier = prefix + id_body
            hits.append(RegexHit(
                surface=surface, entity_type="accession", db=name,
                identifier=identifier, offset=start, length=end - start,
                confidence=conf,
            ))

    hits.sort(key=lambda h: (h.offset, 0 if h.confidence == "high" else 1))
    resolved = []
    used = []
    for h in hits:
        overlap = any(not (h.offset + h.length <= s or h.offset >= e)
                      for s, e in used)
        if not overlap:
            resolved.append(h)
            used.append((h.offset, h.offset + h.length))
    return resolved
