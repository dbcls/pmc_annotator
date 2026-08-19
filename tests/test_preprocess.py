"""
前処理 (JATS XML -> Document) の動作確認.
HunFlair2 のロード不要なので、ライブラリ未インストール環境でも実行可能。
"""
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pmc_annotator.preprocess import JATSParser


def test_basic_parsing():
    sample = Path(__file__).parent / "sample_pmc.xml"
    parser = JATSParser()
    doc = parser.parse(sample)

    assert doc is not None
    assert doc.id == "PMC9999999"
    assert doc.pmid == "99999999"
    assert doc.doi == "10.1000/test.2024"

    section_types = [p.infon.get("section_type") for p in doc.passages]
    print(f"\nSection types: {section_types}")
    print(f"# passages: {len(doc.passages)}")

    for i, p in enumerate(doc.passages):
        print(f"\n[Passage {i}] offset={p.offset} type={p.infon.get('section_type')}")
        print(f"  text: {p.text[:120]}...")

    offsets = [p.offset for p in doc.passages]
    assert offsets == sorted(offsets), "passage offsets must be sorted"

    assert any(p.infon.get("section_type") == "title" for p in doc.passages)
    assert any(p.infon.get("section_type") == "fig_caption" for p in doc.passages)

    bioc = doc.to_bioc()
    assert bioc["id"] == "PMC9999999"
    assert "passages" in bioc
    print(f"\nBioC-JSON keys: {list(bioc.keys())}")
    print(f"First passage BioC: {bioc['passages'][0]}")


if __name__ == "__main__":
    test_basic_parsing()
    print("\n✓ test_basic_parsing passed")
