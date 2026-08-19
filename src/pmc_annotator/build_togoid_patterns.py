"""
build_togoid_patterns.py

TogoID の dataset 定義 (togoid_dataset.yaml) から、文献本文抽出用の
正規表現パターン定義 (togoid_extract_patterns.yaml) を生成する。

TogoIDのregexは ^...$ アンカー付きの「ID検証用」。これを文中から
identifier を見つける「抽出用」に変換し、ノイズ誤検出テストで
3層 (safe / context_required / excluded) に仕分けする。

使い方:
  python build_togoid_patterns.py \
      --input togoid_dataset.yaml \
      --output src/pmc_annotator/data/togoid_extract_patterns.yaml
"""
from __future__ import annotations
import argparse
import re
import yaml
from pathlib import Path
import json

# 実データで観測された誤検出語彙 (否定テスト辞書)
NOISE_VOCAB = []
#_vocab_path = Path(__file__).parent / "noise_vocab.json"
_vocab_path = Path(__file__).parent / "noise_vocab_clean.json"
if _vocab_path.exists():
    NOISE_VOCAB = json.load(open(_vocab_path))

# \d+ が貪欲・無制限で数字を巻き込むDB → 本体の桁と版桁を厳密化。
# prefix集合は現行 orig_regex を尊重し、桁と版だけ締める(誤爆分析 refseq_rna より)。
# 本体は 6桁 or 9桁、版は 1〜2桁。NZ_(WGS)は現行維持で締めない。
REGEX_OVERRIDE = {
    "refseq_rna":     r'^(?<id>(?:NM|NR|XM|XR)_\d{6}(?:\d{3})?)(?:\.\d{1,2})?$',
    "refseq_protein": r'^(?<id>[ANXYWZ]P_\d{6}(?:\d{3})?)(?:\.\d{1,2})?$',
    "refseq_genomic": r'^(?<id>(?:AC_|N[CGTW]_)\d{6}(?:\d{3})?|NZ_[A-Z]+\d+)(?:\.\d{1,2})?$',
}

# ---- ノイズ文コーパス (論文に出る数字・記号・一般語) ----
NOISE = [
    "We cultured 12345 cells at 37C for 24h with 100 mM NaCl at pH 7.4.",
    "Figure 2A shows the result of 50 samples collected in 2020 and 2021.",
    "The p-value was 0.001 and n=15 per group (see Table 3, panel B).",
    "Cells were incubated for 48 hours at 5% CO2 in 10 cm dishes.",
    "A total of 384 reads with 99.9% identity over 250 bp were obtained.",
    "Patients aged 18 to 65 (mean 42.3) enrolled between 2015-2019.",
    "The reaction used 2 mM ATP, 5 U enzyme, in 20 uL for 30 min.",
    "Sections of 4 um stained and imaged at 400x magnification.",
    "Data from chromosome 7q21 and locus D17S250 were analyzed.",
    "Primers F1 and R2 amplified a 1.2 kb fragment (lanes 3-8).",
    "The IC50 was 3.4 nM and Km 12 uM. Version 2.7.1 used 64 GB RAM.",
    "Mice (C57BL/6, n=20) received 5 mg/kg for 14 days at 280 nm.",
    "Grade III tumors in 25 of 100 cases showed type IIa pattern.",
    "Samples A1, B2, C3 and lanes 1-10 were run on a 12% gel.",
    "Values ranged 0.5-2.0 with SD 0.3 across 6 replicates (96 wells).",
]

def alt_matches_vocab(pat_str):
    """選択肢が誤検出語彙のいずれかに (完全一致で) マッチするか。
    マッチしたら危険な選択肢 → 除去対象。"""
    try:
        pat = re.compile(r'^(?:' + pat_str + r')$')
    except re.error:
        return True  # コンパイル不可は危険扱い
    for token in NOISE_VOCAB:
        if pat.match(token):
            return True
    return False

def to_py(rgx: str) -> str:
    """検証用regex → Python構文の文中パターン素材へ。"""
    s = rgx.strip()
    if s.startswith("^"):
        s = s[1:]
    if s.endswith("$"):
        s = s[:-1]
    # (?<id>) → (?P<id>) Python名前付きキャプチャ構文
    s = re.sub(r'\(\?<(id\d*)>', r'(?P<\1>', s)
    # 重複キャプチャ名対策: id が複数あれば連番化
    names = re.findall(r'\(\?P<(id\d*)>', s)
    if len(names) != len(set(names)):
        c = [0]
        s = re.sub(r'\(\?P<id\d*>',
                   lambda m: (c.__setitem__(0, c[0] + 1) or f'(?P<id_{c[0]}>'),
                   s)
    return s


def require_leading_optional(py: str) -> str:
    """先頭の省略可能プレフィックス (?:...)? を必須化 (? を除去)。"""
    if not py.startswith("(?:"):
        return py
    depth = 0
    end = -1
    for i, ch in enumerate(py):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end != -1 and end + 1 < len(py) and py[end + 1] == '?':
        return py[:end + 1] + py[end + 2:]
    return py


def strip_outer_noncap(py: str) -> str:
    """外側全体を包む (?:...) を外す。"""
    if py.startswith("(?:") and py.endswith(")"):
        depth = 0
        for i, ch in enumerate(py):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    if i == len(py) - 1:
                        return py[3:-1]
                    break
    return py


def split_top_or(py: str):
    """トップレベルの | で選択肢分解 (深さ0の | のみ、\\ エスケープ考慮)。"""
    parts = []
    depth = 0
    cur = ""
    i = 0
    while i < len(py):
        ch = py[i]
        if ch == '\\':
            cur += py[i:i + 2]
            i += 2
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == '|' and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    parts.append(cur)
    return parts


def test_noise(pat_str: str):
    """\\b で囲んでコンパイルし、ノイズ誤検出集合を返す。コンパイル不可は None。"""
    try:
        pat = re.compile(r'\b' + pat_str + r'\b')
    except re.error:
        return None
    fp = set()
    for ns in NOISE:
        for m in pat.finditer(ns):
            g = m.group(0)
            if g.strip():
                fp.add(g)
            else:
                # 空マッチを生むパターンは危険 (除外対象)
                fp.add("<empty>")
    return fp


def clean_regex(rgx):
    """危険な選択肢を除去してクリーンなパターンを返す。
    返り値: (cleaned or None, removed, total)"""
    py = to_py(rgx)
    py = require_leading_optional(py)
    inner = strip_outer_noncap(py)
    alts = split_top_or(inner)
    safe_alts = []
    removed = 0
    for alt in alts:
        a = alt.strip()
        if not a:
            continue
        fp = test_noise(a)
        if fp is None or fp:
            removed += 1            # ノイズ文で誤検出 or コンパイル不可
        elif alt_matches_vocab(a):
            removed += 1            # 誤検出語彙にマッチ → 除去
        else:
            safe_alts.append(a)
    if not safe_alts:
        return None, removed, len(alts)
    if len(safe_alts) == 1:
        final = safe_alts[0]
        #return safe_alts[0], removed, len(alts)
    else:
        final = "(?:" + "|".join(safe_alts) + ")"
    # ★修正: test_noise()が検証しているのと同じ \b ガードを、
    # 実際にデプロイされるパターンにも適用する。
    # (これまではtest_noise内部でのみ\bが付与され、返り値のcleanedには
    #  付いていなかったため、「検証されたもの」と「本番で使われるもの」が
    #  食い違っていた)
    return r'\b' + final + r'\b', removed, len(alts)

    #return "(?:" + "|".join(safe_alts) + ")", removed, len(alts)


# ─────────────────────────────────────────────────────────────────────────────
# FlyBase 分離
#   TogoID の ensembl_transcript / ensembl_protein / ensembl_gene の regex は
#   Ensembl Genomes メンバー (FlyBase を含む) を束ねた選択肢の和集合になっており、
#   生成パターンに FBtr…/FBpp…/FBgn… が ensembl_* の db 名のまま残る。
#   これを ensembl_* から取り除き、独立 db (flybase_*) として注入し直す。
#   ※ 桁数下限ゲート (digit_floor_gate.py) の FLOOR_SPEC と db 名・構造を一致させる。
# ─────────────────────────────────────────────────────────────────────────────
FLYBASE_ENTRIES = [
    # (db 名, 抽出regex(語境界なし), プローブ, カテゴリ継承元 ensembl_*)
    ("flybase_gene",       r"FBgn\d{7}", "FBgn0000001", "ensembl_gene"),
    ("flybase_transcript", r"FBtr\d{7}", "FBtr0000001", "ensembl_transcript"),
    ("flybase_protein",    r"FBpp\d{7}", "FBpp0000001", "ensembl_protein"),
]
FLYBASE_PROBES = [e[2] for e in FLYBASE_ENTRIES]


def _alt_is_flybase(alt: str) -> bool:
    """選択肢が FlyBase プローブ (FBgn/FBtr/FBpp…) にマッチするか。"""
    try:
        c = re.compile(r'^(?:' + alt + r')$')
    except re.error:
        return False
    return any(c.match(p) for p in FLYBASE_PROBES)


def _strip_flybase_from_pattern(pattern: str):
    """cleaned パターン (\\b…\\b) から FlyBase にマッチする選択肢を除去。
    返り値: (new_pattern | None, removed数)。全選択肢が FlyBase なら (None, n)。"""
    p = pattern
    if p.startswith(r'\b'):
        p = p[2:]
    if p.endswith(r'\b'):
        p = p[:-2]
    inner = strip_outer_noncap(p)
    alts = split_top_or(inner)
    kept = [a for a in alts if not _alt_is_flybase(a)]
    removed = len(alts) - len(kept)
    if removed == 0:
        return pattern, 0
    if not kept:
        return None, removed
    new_inner = kept[0] if len(kept) == 1 else "(?:" + "|".join(kept) + ")"
    return r'\b' + new_inner + r'\b', removed


def split_flybase(result: dict):
    """result を破壊的に更新: ensembl_* 等から FlyBase 選択肢を除去し flybase_* を注入。"""
    moved = 0
    # 1) 既存 safe / context_required から FlyBase 選択肢を除去
    for grp in ("safe", "context_required"):
        for key in list(result[grp].keys()):
            e = result[grp][key]
            pat = e.get("pattern")
            if not pat:
                continue
            new_pat, removed = _strip_flybase_from_pattern(pat)
            if not removed:
                continue
            moved += removed
            if new_pat is None:
                # 全選択肢が FlyBase → このエントリ自体を撤去 (flybase_* に置換されるため)
                del result[grp][key]
            else:
                e["pattern"] = new_pat
                prev = e.get("note", "")
                e["note"] = (prev + "; " if prev else "") + f"removed {removed} FlyBase alt(s) → flybase_*"
    # 2) flybase_* を注入 (カテゴリは ensembl_* の対応 db から継承)
    for key, rgx, _probe, inherit in FLYBASE_ENTRIES:
        cat = "?"
        for grp in ("safe", "context_required", "excluded"):
            if inherit in result[grp]:
                cat = result[grp][inherit].get("category", "?")
                break
        result["safe"][key] = {
            "category": cat,
            "orig_regex": rgx,
            "pattern": r'\b' + rgx + r'\b',
            "note": "injected: FlyBase split from ensembl_* (independent db)",
        }
    return moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="togoid_dataset.yaml")
    ap.add_argument("--output",
                    default="src/pmc_annotator/data/togoid_extract_patterns.yaml")
    args = ap.parse_args()

    with open(args.input) as f:
        data = yaml.safe_load(f)

    result = {"safe": {}, "context_required": {}, "excluded": {}}

    for key, v in data.items():
        if not isinstance(v, dict):
            continue
        rgx = v.get("regex", "")
        if not rgx:
            continue
        if key in REGEX_OVERRIDE:          # refseq系の桁固定オーバーライド
            rgx = REGEX_OVERRIDE[key]
        cat = v.get("category", "?")

        # identifiers.org URI を取得 (prefix リストから)
        curie = None
        for p in v.get("prefix", []) or []:
            if isinstance(p, dict):
                label = str(p.get("label", "")).lower()
                if "identifiers.org" in label:
                    curie = p.get("uri")

        cleaned, removed, total = clean_regex(rgx)
        entry = {"category": cat, "orig_regex": rgx}
        if curie:
            entry["uri"] = curie

        if cleaned is None:
            result["excluded"][key] = entry
        else:
            entry["pattern"] = cleaned
            if removed > 0:
                entry["note"] = f"{total - removed}/{total} alternatives kept"
            result["safe"][key] = entry

    # FlyBase を ensembl_* から分離し独立 db として注入
    fb_moved = split_flybase(result)

    # 最終検証: safe 全パターンがノイズ誤検出ゼロか
    bad = 0
    for key, e in result["safe"].items():
        fp = test_noise(e["pattern"])
        if fp:
            bad += 1
            print(f"  [warn] 残留誤検出 {key}: {fp}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(result, f, allow_unicode=True, sort_keys=True,
                  default_flow_style=False)

    print(f"=== 生成完了 ===")
    print(f"  FlyBase 分離: {fb_moved} 選択肢を ensembl_* から除去 → flybase_gene/transcript/protein を注入")
    print(f"  safe: {len(result['safe'])}")
    print(f"  context_required: {len(result['context_required'])}")
    print(f"  excluded: {len(result['excluded'])}")
    print(f"  残留誤検出: {bad} 種")
    print(f"  → {out_path}")


if __name__ == "__main__":
    main()
