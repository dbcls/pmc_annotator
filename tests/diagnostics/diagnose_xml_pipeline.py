"""
preprocess の透過性検証.

目的:
  - 元XMLバイト列にアンダースコアが何個あるか
  - lxml でパース後、要素内テキストにアンダースコアが何個あるか
  - preprocess (JATSParser) 後の passage.text にアンダースコアが何個あるか
  - どの段階で `_` が失われているか (or 置換されているか) を特定

使い方:
  python tests/diagnose_xml_pipeline.py \
      --xml /home/yayamamo/PMC_xml/PMC000xxxxxx/PMC512530.xml
"""
import sys
import argparse
import re
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def count_underscores(text: str) -> dict:
    """テキスト中の `_` 文字、`\\b\\w+_\\w+\\b` 風トークンをカウント"""
    return {
        "raw_underscore_chars": text.count("_"),
        "underscored_tokens": len(re.findall(r"\w+_\w+", text)),
        "sample_tokens": re.findall(r"\w+_\w+", text)[:10],
    }


def stage1_raw_bytes(xml_path: Path):
    """元 XML のバイト列に `_` がいくつあるか"""
    raw = xml_path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    print(f"[Stage 1] 元XMLバイト列")
    print(f"  ファイルサイズ: {len(raw):,} bytes")
    c = count_underscores(text)
    print(f"  '_' 文字数: {c['raw_underscore_chars']}")
    print(f"  アンダースコアトークン: {c['underscored_tokens']} 個")
    print(f"  サンプル: {c['sample_tokens']}")
    return c


def stage2_lxml_raw(xml_path: Path):
    """lxml でパースして、全 itertext を結合した結果"""
    from lxml import etree
    print(f"\n[Stage 2] lxml.etree.parse + 全 itertext 結合")
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    all_text = "".join(t for t in root.itertext() if t)
    c = count_underscores(all_text)
    print(f"  '_' 文字数: {c['raw_underscore_chars']}")
    print(f"  アンダースコアトークン: {c['underscored_tokens']} 個")
    print(f"  サンプル: {c['sample_tokens']}")
    return c


def stage3_lxml_body_only(xml_path: Path):
    """body 配下だけ抽出 (JATSParserに近い構造)"""
    from lxml import etree
    print(f"\n[Stage 3] body 配下の itertext のみ")
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    body = root.find(".//body")
    if body is None:
        print(f"  body 要素なし")
        return None
    all_text = "".join(t for t in body.itertext() if t)
    c = count_underscores(all_text)
    print(f"  '_' 文字数: {c['raw_underscore_chars']}")
    print(f"  アンダースコアトークン: {c['underscored_tokens']} 個")
    print(f"  サンプル: {c['sample_tokens']}")
    return c


def stage4_preprocess(xml_path: Path):
    """JATSParser を通した後の passage.text 結合"""
    from pmc_annotator.preprocess import JATSParser
    print(f"\n[Stage 4] JATSParser を通した passage.text 結合")
    parser = JATSParser()
    doc = parser.parse(xml_path)
    if doc is None:
        print(f"  パース失敗")
        return None
    all_text = "\n".join(p.text for p in doc.passages)
    c = count_underscores(all_text)
    print(f"  passage 数: {len(doc.passages)}")
    print(f"  結合テキスト長: {len(all_text):,} chars")
    print(f"  '_' 文字数: {c['raw_underscore_chars']}")
    print(f"  アンダースコアトークン: {c['underscored_tokens']} 個")
    print(f"  サンプル: {c['sample_tokens']}")
    return c


def stage5_search_specific(xml_path: Path):
    """既知の特定トークンをXMLバイト列で grep して、存在を確認"""
    print(f"\n[Stage 5] 特定トークンの存在検索 (バッチログから)")
    raw = xml_path.read_bytes().decode("utf-8", errors="replace")
    target_tokens = [
        "blau_map", "blau-map",
        "CITB_361N2", "CITB-361N2",
        "RPCI-11_147B17", "RPCI-11-147B17",
        "NM_022162", "NM-022162",
    ]
    for tok in target_tokens:
        count = raw.count(tok)
        print(f"  {tok!r}: {count} 回出現")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", type=Path, required=True)
    args = ap.parse_args()

    print(f"対象: {args.xml}\n")

    c1 = stage1_raw_bytes(args.xml)
    c2 = stage2_lxml_raw(args.xml)
    c3 = stage3_lxml_body_only(args.xml)
    c4 = stage4_preprocess(args.xml)
    stage5_search_specific(args.xml)

    print("\n" + "=" * 78)
    print("判定")
    print("=" * 78)
    if c1 and c2 and c1["raw_underscore_chars"] > 0 and c2["raw_underscore_chars"] == 0:
        print("  ✗ lxml の itertext で `_` が失われている可能性")
    elif c2 and c4 and c2["raw_underscore_chars"] > 0 and c4["raw_underscore_chars"] == 0:
        print("  ✗ JATSParser の処理で `_` が失われている可能性")
    elif c1 and c1["raw_underscore_chars"] == 0:
        print("  ! 元XMLバイト列にすでに `_` が無い (XMLエンティティ化されている可能性)")
        print("    → バッチログの 'blau_map.pdf' は、splitter が `blau-map.pdf` を ")
        print("      正規化して 'blau_map.pdf' に戻している (?) という奇妙な状況")
        print("      もしくはバッチログとPMC512530のXMLは別物?")
    elif c1 and c4 and c1["raw_underscore_chars"] == c4["raw_underscore_chars"]:
        print("  ✓ どの段階でも `_` 数が保存されている")
    else:
        print(f"  Stage 1: {c1['raw_underscore_chars'] if c1 else '?'}")
        print(f"  Stage 2: {c2['raw_underscore_chars'] if c2 else '?'}")
        print(f"  Stage 3: {c3['raw_underscore_chars'] if c3 else '?'}")
        print(f"  Stage 4: {c4['raw_underscore_chars'] if c4 else '?'}")


if __name__ == "__main__":
    main()
