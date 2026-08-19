"""
PMC512530 (splitter警告が大量に出る論文) で splitter とオフセットの整合性を診断する.

確認したい3点:
  1. preprocess 後の passage.text にアンダースコア (`NM_022162`, `blau_map` 等) が
     保存されているか — preprocess 段階で壊れていないか
  2. SciSpacySentenceSplitter が返す各 Sentence について:
     - sentence.text が元 passage.text と一致するか (正規化されているか)
     - sentence.start_position が元 passage.text 内の正しい位置か
  3. HunFlair2 NER の結果で:
     - アノテーションの text が元テキストと一致するか
     - "NM_022162" のような accession 周辺で NER スパンがどう扱われるか

使い方:
  cd <repo>/pmc_annotator
  python tests/diagnose_splitter.py \
      --xml /home/yayamamo/PMC_xml/PMC000xxxxxx/PMC512530.xml \
      --device cuda:2

GPU不要の検査だけしたい場合 (1.と2.のみ):
  python tests/diagnose_splitter.py --xml ... --no-tagger
"""
import sys
import argparse
import re
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pmc_annotator.preprocess import JATSParser


# 注目したいトークン (アンダースコアを含む accession 系)
SUSPICIOUS_PATTERNS = [
    r'\b[A-Z]+_\d+(\.\d+)?\b',          # NM_022162, NP_001234 等
    r'\bCITB_\d+[A-Z]\d+\b',            # CITB_361N2 等
    r'\bRPCI-\d+_\d+[A-Z]\d+\b',        # RPCI-11_147B17 等
    r'\b[a-z]+_map\.pdf\b',             # blau_map.pdf 等
    r'\b[A-Z]+_[A-Z]+\b',               # FOO_BAR (generic)
]

SUSPICIOUS_RE = re.compile("|".join(f"({p})" for p in SUSPICIOUS_PATTERNS))


def find_underscored_tokens(text: str, label: str = "") -> list[tuple[int, str]]:
    """テキスト中のアンダースコア入りトークンとそのオフセットを列挙"""
    hits = []
    for m in SUSPICIOUS_RE.finditer(text):
        hits.append((m.start(), m.group()))
    return hits


# ===== 検査1: preprocess段階での保存 =====

def check_preprocess(xml_path: Path):
    print("=" * 78)
    print(f"[検査1] preprocess後のpassage.textに `_` が保存されているか")
    print("=" * 78)
    parser = JATSParser()
    doc = parser.parse(xml_path)
    if doc is None:
        print("  前処理失敗")
        sys.exit(1)

    n_chars = sum(len(p.text) for p in doc.passages)
    print(f"  doc.id={doc.id}, passages={len(doc.passages)}, total_chars={n_chars}")

    total_hits = 0
    sample_hits = []
    for p_idx, passage in enumerate(doc.passages):
        hits = find_underscored_tokens(passage.text)
        if hits:
            total_hits += len(hits)
            for off, tok in hits[:3]:
                sample_hits.append((p_idx, passage.infon.get("section_type", "?"), off, tok))

    print(f"  アンダースコア入りトークン: {total_hits} 件")
    print(f"  サンプル (passage_idx, sec_type, offset_in_passage, token):")
    for p_idx, sec, off, tok in sample_hits[:15]:
        print(f"    p{p_idx} [{sec}] @{off}: {tok!r}")

    if total_hits == 0:
        print("  ⚠ アンダースコアが preprocess 段階で消えている? 要確認")
    else:
        print("  ✓ アンダースコアは preprocess を通過している")

    return doc


# ===== 検査2: splitter が返す sentence の挙動 =====

def check_splitter(doc):
    print("\n" + "=" * 78)
    print(f"[検査2] SciSpacySentenceSplitter の sentence.text と start_position")
    print("=" * 78)
    try:
        from flair.splitter import SciSpacySentenceSplitter
    except ImportError:
        print("  flair 未インストールのためスキップ")
        return None

    splitter = SciSpacySentenceSplitter()

    # アンダースコアトークンが含まれる passage を狙って検査
    target_passage = None
    for p in doc.passages:
        if "_" in p.text and re.search(r'\w_\w', p.text):
            target_passage = p
            break

    if target_passage is None:
        print("  アンダースコアを含む passage が無いので、最初の passage で検査")
        target_passage = doc.passages[0]

    sec = target_passage.infon.get("section_type", "?")
    print(f"  対象passage: section_type={sec}, length={len(target_passage.text)}")

    sents = splitter.split(target_passage.text)
    print(f"  分割数: {len(sents)} 文")

    # 元passage内のアンダースコアトークンを取得
    orig_hits = {off: tok for off, tok in find_underscored_tokens(target_passage.text)}
    print(f"  passage内アンダースコアトークン: {len(orig_hits)} 件")

    # 各 sentence について、 start_position と text が正しいか検証
    text_matches = 0
    text_mismatches = 0
    pos_matches = 0
    pos_mismatches = 0
    sample_mismatches = []

    for s in sents:
        start = s.start_position
        end = start + len(s.text)
        original_slice = target_passage.text[start:end]

        # sentence.text と original_slice の比較
        if s.text == original_slice:
            text_matches += 1
            pos_matches += 1
        else:
            text_mismatches += 1
            # オフセットが正しいか別の見方で検証: 元テキスト内で sentence.text を探す
            if s.text in target_passage.text:
                pos_matches += 1
            else:
                pos_mismatches += 1
            if len(sample_mismatches) < 5:
                # 違いをハイライト
                diff_chars = sum(1 for a, b in zip(s.text, original_slice) if a != b)
                sample_mismatches.append({
                    "sent_text": s.text[:150],
                    "orig_slice": original_slice[:150],
                    "diff_chars": diff_chars,
                    "start": start,
                })

    print(f"  text一致(sentence.text == passage.text[start:end]): {text_matches}")
    print(f"  text不一致 (= splitterが正規化): {text_mismatches}")
    print(f"  start_position有効 (sentence.text が passage内に存在): {pos_matches}")
    print(f"  start_position破損 (元テキストにsentence.textが見当たらない): {pos_mismatches}")

    if text_mismatches > 0:
        print(f"\n  text不一致の例 (最大5件):")
        for i, m in enumerate(sample_mismatches):
            print(f"\n    [{i}] start={m['start']}, diff_chars={m['diff_chars']}")
            print(f"      sentence.text   : {m['sent_text']!r}")
            print(f"      passage[s:s+len]: {m['orig_slice']!r}")

    # 結論
    print("\n  結論:")
    if text_mismatches == 0:
        print("  ✓ splitter は元テキストを保存している")
    elif pos_matches == len(sents):
        print("  ⚠ splitter が text を正規化しているが、start_position は元テキスト基準")
        print("     → オフセット計算は問題なし。ただし sentence.text を直接使う場面で要注意")
    else:
        print("  ✗ start_position が破損 → オフセット計算が壊れる可能性")

    return sents


# ===== 検査3: HunFlair2 NER 結果のオフセット整合性 =====

def check_full_pipeline(xml_path: Path, device: str):
    print("\n" + "=" * 78)
    print(f"[検査3] フルパイプライン: NER結果のアノテーション text とオフセット整合性")
    print("=" * 78)
    from pmc_annotator.annotate_hunflair import HunFlairAnnotator

    parser = JATSParser()
    doc = parser.parse(xml_path)

    annotator = HunFlairAnnotator(device=device, enable_linkers=False)
    annotator.load()
    print(f"  HunFlair2 ロード完了")

    doc = annotator(doc)
    n_ann = sum(len(p.annotations) for p in doc.passages)
    print(f"  アノテーション数: {n_ann}")

    # 全アノテーションのオフセット整合性検証
    ft = doc.full_text()
    ok = 0
    ng_mismatches = []

    for p in doc.passages:
        for a in p.annotations:
            sliced = ft[a.offset:a.offset + a.length]
            if sliced == a.text:
                ok += 1
            else:
                ng_mismatches.append({
                    "ann_text": a.text,
                    "ft_slice": sliced,
                    "offset": a.offset,
                    "length": a.length,
                    "entity_type": a.entity_type,
                })

    print(f"  OK: {ok}")
    print(f"  NG: {len(ng_mismatches)}")

    if ng_mismatches:
        print(f"\n  NG事例 (最大10件):")
        for i, m in enumerate(ng_mismatches[:10]):
            # 1文字違い? なんかの正規化?
            diff = sum(1 for a, b in zip(m['ann_text'], m['ft_slice']) if a != b)
            print(f"    [{i}] offset={m['offset']} len={m['length']} type={m['entity_type']}")
            print(f"        ann.text      : {m['ann_text']!r}")
            print(f"        full_text[s:e]: {m['ft_slice']!r}")
            print(f"        differs by {diff} characters")

    # アンダースコア入りアノテーションの個別チェック
    print(f"\n  アンダースコア入りアノテーション:")
    underscore_anns = []
    for p in doc.passages:
        for a in p.annotations:
            if "_" in a.text:
                underscore_anns.append(a)
    if underscore_anns:
        for a in underscore_anns[:10]:
            ft_slice = ft[a.offset:a.offset + a.length]
            match = "✓" if ft_slice == a.text else "✗"
            print(f"    {match} {a.text!r} (offset={a.offset}, type={a.entity_type}, "
                  f"full_text@offset={ft_slice!r})")
    else:
        print(f"    (アンダースコア入りアノテーションは無し)")

    # 元XMLにあるアクセッション類が拾えているか確認
    print(f"\n  元passageに含まれるアンダースコア入りトークン vs NER捕捉:")
    all_underscored = []
    for p_idx, p in enumerate(doc.passages):
        for off, tok in find_underscored_tokens(p.text):
            global_off = p.offset + off
            all_underscored.append((global_off, tok))

    if all_underscored:
        # 各トークンと重なるアノテーションを探す
        all_anns = [(a.offset, a.offset + a.length, a.text, a.entity_type)
                    for p in doc.passages for a in p.annotations]
        for off, tok in all_underscored[:15]:
            end = off + len(tok)
            overlapping = [a for a in all_anns if not (a[1] <= off or a[0] >= end)]
            if overlapping:
                summary = ", ".join(f"{a[2]!r}({a[3]})" for a in overlapping[:3])
                print(f"    {tok!r} @{off}: NER ovl = {summary}")
            else:
                print(f"    {tok!r} @{off}: NER ovl = (none, accessionは後段RegexAnnotator対象)")
    else:
        print(f"    (アンダースコア入りトークンが見つからず)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", type=Path, required=True)
    ap.add_argument("--device", default="cuda:2")
    ap.add_argument("--no-tagger", action="store_true",
                    help="検査3 (HunFlair2 タガー実行) をスキップ")
    args = ap.parse_args()

    print(f"対象: {args.xml}\n")

    doc = check_preprocess(args.xml)
    check_splitter(doc)
    if not args.no_tagger:
        check_full_pipeline(args.xml, args.device)

    print("\n" + "=" * 78)
    print("検査完了。判定の指針:")
    print("  - 検査1: アンダースコアが preprocess を通過していれば preprocess 側は問題なし")
    print("  - 検査2: text不一致が出ても、start_position有効なら OK (オフセット計算は無事)")
    print("  - 検査3: NG=0 なら本番投入可能。アクセッションは別途RegexAnnotator対象")
    print("=" * 78)


if __name__ == "__main__":
    main()
