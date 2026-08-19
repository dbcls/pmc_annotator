#!/usr/bin/env python3
"""
桁数下限ゲート — 3層ゲートの第1層(統計的・安価)

feasibility_memo.md §5 第1層 / §7 実装#1 の実体。
ENA/PDB 抽出器(#2,#4)の投入より **先に** 入れるべき前提コンポーネント。

━━ 何をするか ━━
各 db のローカルID(CURIE の接頭辞を除いた部分)が、そのアーカイブが実運用で
用いる **最小桁数を含む構造** に合致するかを判定する。合致しないものは
「接頭辞に似た文字列 + 少数の数字」= 桁数無制約 regex 由来の系統的偽陽性
として棄却する。

━━ なぜレジストリではなくこれか(否定的知見 §4) ━━
identifiers.org の公式パターンは「解決可能なIDの構文的妥当性」を定義するもので、
`rs2`・`SRP1001` を正当と見なす。本文抽出での尤度判定には使えない。よって桁数下限は
**アーカイブの実運用アクセッション幅** から決める(下記 FLOOR_SPEC の各 note を参照)。

━━ 検証(このファイル内の --self-test) ━━
監査で観測された 21 件の偽陽性を全件棄却し、真陽性の損失ゼロ、を assert する
(グラウンドトゥルースは deposit_audit.py 由来、fetch_idorg_patterns.py の OBSERVED と同一)。

━━ 使い方 ━━
  # 検証(21件棄却 / TP損失0 を確認)
  python digit_floor_gate.py --self-test

  # 既存 parquet 出力に後付け適用(パイプライン再実行不要)
  #   デフォルト: gate_pass / gate_reason 列を付与して書き出し + db別 before/after
  python digit_floor_gate.py accession_annotations_togoid --out accession_annotations_gated
  #   通過行のみ残す(縮約)
  python digit_floor_gate.py accession_annotations_togoid --out … --drop
  #   書き出さず集計レポートだけ
  python digit_floor_gate.py accession_annotations_togoid --report-only

━━ build_togoid_patterns.py からの利用 ━━
  from digit_floor_gate import local_pattern, floor_of, passes_gate
  抽出時に regex を締める用途には local_pattern(db)(^$なし・境界は呼び出し側で付与)、
  抽出後の後段フィルタには passes_gate(db, local_id)。二重の防壁として両方で使える。
"""
import re
import os
import sys
import glob
import argparse
from collections import Counter, OrderedDict

# ─────────────────────────────────────────────────────────────────────────────
# FLOOR_SPEC : db -> ゲート仕様
#   pattern : ローカルID(接頭辞除去後)を fullmatch する正規表現(^$は付けない)
#   kind    : "hard" = 桁数下限がこの層で有効に効き、単独で偽陽性を落とせる
#             "soft" = 構造検証のみ。桁数下限が原理的に効かず下流(文脈/実在性)が必須
#             "pass" = この層では手を入れない(構造も緩く、後段に委ねる)
#   floor   : 適用した桁数下限(記録・レポート用。パターン内にも反映済み)
#   note    : 桁数下限の根拠(= アーカイブの実運用幅)
#
# ★ SRA/INSDC-SRA の 6 サブタイプは [EDS]R<T>\d{6,} を共有する。
#   T: A=submission, P=study/project, R=run, S=sample, X=experiment, Z=analysis
#   接頭辞 E/S/D は ENA/NCBI-SRA/DDBJ。db 名側で型は既に分かれているので T で固定。
# ─────────────────────────────────────────────────────────────────────────────

# 当面抽出しない db(抽出器を生成せず、ゲートでも常に棄却する)。
#   togovar: tgv1/tgv2 の偽陽性かつ本質は実在性問題。方針として当面は抽出対象外。
EXCLUDED_DBS = {"togovar"}

_SRA_TYPE = {  # db 名 -> 型文字 T
    "sra_accession":  "A",
    "sra_project":    "P",
    "sra_run":        "R",
    "sra_sample":     "S",
    "sra_experiment": "X",
    "sra_analysis":   "Z",
}

FLOOR_SPEC = OrderedDict()

# --- SRA 系(hard, floor=6) -------------------------------------------------
#   実運用: アクセッションは常に 6 桁ゼロ詰め(SRR000001)。近年 7 桁以上へ拡張。
#   観測FP は全て ≤5 桁(SRX07711, SRS30216, ERR9555, SRP1001, ERP0206, DRA0277…)。
for _db, _T in _SRA_TYPE.items():
    FLOOR_SPEC[_db] = {
        "pattern": rf"[EDS]R{_T}\d{{6,}}",
        "kind": "hard", "floor": 6,
        "note": "SRA は常に6桁ゼロ詰め。観測FPは全て≤5桁で、floor=6が真陽性を落とさず分離",
    }

# --- dbSNP(hard, floor=4) --------------------------------------------------
#   rs1..rs168 は表参照・残基番号。真の rs 番号は初期でも4桁以上。
#   ※保守的に4。低番号の正当な rs には損失余地があるため実データで要確認(§5 トレードオフ)。
FLOOR_SPEC["dbsnp"] = {
    "pattern": r"rs\d{4,}",
    "kind": "hard", "floor": 4,
    "note": "rs1..rs168 は残基番号/表参照。floor=4 で観測FP全棄却・TP(rs1339067等)保持",
}

# --- BioSample(hard, floor=6) ----------------------------------------------
#   ★メモ §5 の SAM[NED]\d{8,} は EBI の SAMEA…(SAM+E+A+digits)を全棄却してしまう。
#   ソース文字を [A-Z]? で許容し floor=6 に。SAMD9 等の遺伝子名(≤2桁)は棄却、
#   SAMN########(NCBI,8桁)/ SAMEA######(EBI,6-8桁)/ SAMD########(DDBJ,8桁)は保持。
FLOOR_SPEC["biosample"] = {
    "pattern": r"SAM[NED][A-Z]?\d{6,}",
    "kind": "hard", "floor": 6,
    "note": "SAMD9等の遺伝子名は≤2桁で棄却。EBI SAMEA を落とさぬよう [A-Z]? を許容(memo版の修正)",
}

# --- INSDC ヌクレオチド(ENA)/ アセンブリ(hard-ish, 構造固定) ----------------
#   桁数下限より「文字数×桁数の型」で締まる。TP: AF011567/AY057379/AB020640。
FLOOR_SPEC["ena"] = {
    "pattern": r"(?:[A-Z]\d{5}|[A-Z]{2}\d{6}|[A-Z]{4}\d{8,10}|[A-J][A-Z]{2}\d{5})(?:\.\d+)?",
    "kind": "hard", "floor": None,
    "note": "INSDC は文字数×桁数で型固定。桁数下限は型内に内包済み",
}
FLOOR_SPEC["assembly_insdc"] = {
    "pattern": r"GC[AF]_\d{9}(?:\.\d+)?",
    "kind": "hard", "floor": 9,
    "note": "GCA_/GCF_ は9桁固定(+版)",
}

# --- BioProject(hard, floor=1 だが構造で締まる) -----------------------------
#   PRJ(NA|EB|DB|EA|DA…)+数字。低番号(PRJNA1)が実在するため桁数下限は課さず構造のみ。
FLOOR_SPEC["bioproject"] = {
    "pattern": r"PRJ[EDN][A-Z]\d+",
    "kind": "hard", "floor": None,
    "note": "低番号(PRJNA1)が実在。桁数下限は課さず PRJ[EDN][A-Z] の構造で締める",
}

# --- GEO(pass:桁数下限を課さない) ------------------------------------------
#   GSE1/GSM1 等の低番号が実在(初期シリーズ)。観測FPも無い(fp:[])。構造のみ。
FLOOR_SPEC["geo_series"] = {"pattern": r"GSE\d+", "kind": "pass", "floor": None,
                            "note": "GSE1 等の低番号が実在。桁数下限を課すと真陽性を落とす"}
FLOOR_SPEC["geo_sample"] = {"pattern": r"GSM\d+", "kind": "pass", "floor": None,
                            "note": "GSM の低番号も実在。構造のみ"}
FLOOR_SPEC["geo_platform"] = {"pattern": r"GPL\d+", "kind": "pass", "floor": None,
                              "note": "同上"}
FLOOR_SPEC["arrayexpress"] = {"pattern": r"E-[A-Z]{4}-\d+", "kind": "pass", "floor": None,
                              "note": "E-GEOD-1073 等。番号は低番号もあり構造のみ"}

# --- 参照リソース(構造検証。桁数下限は型に内包) ------------------------------
FLOOR_SPEC["pfam"]    = {"pattern": r"PF\d{5}",  "kind": "hard", "floor": 5,
                         "note": "PF+5桁固定"}
FLOOR_SPEC["interpro"] = {"pattern": r"IPR\d{6}", "kind": "hard", "floor": 6,
                          "note": "IPR+6桁固定"}
FLOOR_SPEC["refseq_rna"]     = {"pattern": r"[NX][MR]_\d{6,}(?:\.\d+)?", "kind": "hard", "floor": 6,
                                "note": "NM_/NR_/XM_/XR_ + 6桁以上"}
FLOOR_SPEC["refseq_protein"] = {"pattern": r"(?:AP|[NXWYZ]P)_\d{6,}(?:\.\d+)?",  "kind": "hard", "floor": 6,
                                "note": "NP_/XP_/WP_/YP_/ZP_/AP_ + 6桁以上。ZP_ は2009年代WGS由来タンパクで大量(旧サブセットに多い)"}
FLOOR_SPEC["refseq_genomic"] = {"pattern": r"(?:N[CGTWZ]_\d{6,}|N[CZ]_[A-Z]{4}\d{6,})(?:\.\d+)?", "kind": "hard", "floor": 6,
                                "note": "NC_/NG_/NT_/NW_/NZ_ + 6桁。加えて WGS 形式 NZ_/NC_ + [A-Z]{4} + 数字(NZ_AAEP00000000)も許容"}
FLOOR_SPEC["uniprot"] = {
    "pattern": r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-\d{1,2})?(?:\.\d+)?",
    "kind": "hard", "floor": None,
    "note": "UniProt 6/10文字型。アイソフォーム -N(1-2桁)と版 .N を、単独でも複合(P70658-2.7)でも許容。"
            "-3002/-848 等の非現実的接尾(残基範囲等)は上限2桁で棄却",
}
FLOOR_SPEC["ensembl_transcript"] = {"pattern": r"ENS[A-Z]{0,8}T\d{11}(?:\.\d+)?", "kind": "hard", "floor": 11,
                                    "note": "ENS…T + 11桁。種コード/データセット接尾(EST等)を [A-Z]{0,8} で許容(ENSANGESTT…)"}
FLOOR_SPEC["ensembl_protein"]    = {"pattern": r"ENS[A-Z]{0,8}P\d{11}(?:\.\d+)?", "kind": "hard", "floor": 11,
                                    "note": "ENS…P + 11桁。同上(ENSXETESTP…)"}

# --- FlyBase(Ensembl形式ではない独立名前空間。ensembl_* から分離)-----------------
#   FBtr/FBpp/FBgn + 7桁ゼロ詰め。抽出器(build_togoid_patterns.py)側も、従来
#   ensembl_transcript/ensembl_protein としてタグしていた FBtr/FBpp を、この db 名で
#   タグし直す必要がある(ゲートは検証のみで再分類はしない)。
FLOOR_SPEC["flybase_transcript"] = {"pattern": r"FBtr\d{7}", "kind": "hard", "floor": 7,
                                    "note": "FlyBase 転写産物 FBtr + 7桁。ensembl_transcript から分離"}
FLOOR_SPEC["flybase_protein"]    = {"pattern": r"FBpp\d{7}", "kind": "hard", "floor": 7,
                                    "note": "FlyBase タンパク FBpp + 7桁。ensembl_protein から分離"}
FLOOR_SPEC["flybase_gene"]       = {"pattern": r"FBgn\d{7}", "kind": "hard", "floor": 7,
                                    "note": "FlyBase 遺伝子 FBgn + 7桁。文献で使われるが概念/エンティティ言及"
                                            "=寄託指標では対象外(ensembl_gene 同様)。構造検証のみ行う"}

# --- 桁数下限が原理的に効かない / 値域問題(soft:この層では通すが下流必須) -------
#   PDB: 4文字([0-9][A-Za-z0-9]{3})。桁数ゲート不能(§6)。文脈+実在性が必須。
FLOOR_SPEC["pdb"] = {
    "pattern": r"[0-9][A-Za-z0-9]{3}",
    "kind": "soft", "floor": None,
    "note": "4文字。桁数下限は原理的に無効。第2層(文脈)+第3層(実在性)が必須",
}
#   nbdc_human_db: 4桁ちょうど(hum0001 形式)を構造として固定する。
#   ※ hum6813 のような「4桁だが実在しない番号帯」は桁数では分離不可。この層は構造のみ
#     担保し、値域外は第3層(実在性検証)に委ねる = kind=soft。
FLOOR_SPEC["nbdc_human_db"] = {
    "pattern": r"hum[0-9]{4}",
    "kind": "soft", "floor": 4,
    "note": "hum0001 形式の4桁固定。hum1/hum12345 等の構造不一致は棄却。"
            "hum6813 等の値域外(4桁だが実在せず)は桁数では分離不可 → 実在性検証に委ねる",
}

# コンパイル済み fullmatch パターン
_COMPILED = {db: re.compile(spec["pattern"]) for db, spec in FLOOR_SPEC.items()}


# ─────────────────────────────────────────────────────────────────────────────
# 公開 API
# ─────────────────────────────────────────────────────────────────────────────
def strip_prefix(identifier):
    """CURIE 'db:localid' -> 'localid'。接頭辞が無ければそのまま。"""
    if identifier is None:
        return None
    s = str(identifier)
    return s.split(":", 1)[1] if ":" in s else s


def local_pattern(db):
    """db のローカルID用パターン文字列(^$なし)。build_togoid_patterns.py が
    抽出regexを締める際に境界を付けて使う。未定義db・抽出対象外dbは None。"""
    if db in EXCLUDED_DBS:
        return None
    spec = FLOOR_SPEC.get(db)
    return spec["pattern"] if spec else None


def floor_of(db):
    """db に適用した桁数下限(無ければ None)。"""
    spec = FLOOR_SPEC.get(db)
    return spec["floor"] if spec else None


def passes_gate(db, local_id):
    """(passed: bool, reason: str) を返す。

    - db が未定義: 手を入れない方針で通過(reason='no-rule')。監査していないdbで
      真陽性を壊さないための保守的既定。フルセット導入前に対象dbを確定すること。
    - kind='pass': 構造のみ検査。空/構造不一致なら棄却、合致すれば通過。
    - kind='hard': 構造+桁数下限に fullmatch すれば通過、しなければ棄却(floor)。
    - kind='soft': 構造に合致すれば「この層は通過」だが reason='soft-needs-downstream'
      を返し、第2/第3層が必須であることを明示。
    """
    if local_id is None:
        return False, "empty"
    s = str(local_id).strip()
    if s == "":
        return False, "empty"

    if db in EXCLUDED_DBS:
        return False, "excluded"

    spec = FLOOR_SPEC.get(db)
    if spec is None:
        return True, "no-rule"

    ok = _COMPILED[db].fullmatch(s) is not None
    kind = spec["kind"]
    if kind == "soft":
        # 構造は満たすが下流必須。構造も満たさなければ棄却。
        return (True, "soft-needs-downstream") if ok else (False, "structure")
    if kind == "pass":
        return (True, "pass-structure") if ok else (False, "structure")
    # hard
    return (True, "pass") if ok else (False, f"floor<{spec['floor']}")


# ─────────────────────────────────────────────────────────────────────────────
# 監査グラウンドトゥルース(deposit_audit.py 由来 / OBSERVED と同一)
# ─────────────────────────────────────────────────────────────────────────────
OBSERVED = {
    "dbsnp":         {"fp": ["rs1","rs2","rs3","rs6","rs24","rs48","rs120","rs168"],
                      "tp": ["rs1339067","rs1048771"]},
    "biosample":     {"fp": ["SAMDC1","SAME40","SAMD4","SAMD9"], "tp": []},
    "sra_project":   {"fp": ["SRP1001","ERP2518","ERP0206"],     "tp": ["SRP000031","SRP000799"]},
    "sra_run":       {"fp": ["ERR9555"],                          "tp": ["SRR003196","ERR001087"]},
    "sra_experiment":{"fp": ["ERX13412","SRX07711"],              "tp": ["SRX007710","SRX001455"]},
    "sra_sample":    {"fp": ["SRS30216"],                         "tp": ["SRS003157"]},
    "sra_accession": {"fp": ["DRA0277","DRA0311"],                "tp": ["DRA030922"]},
    "geo_series":    {"fp": [],  "tp": ["GSE1073","GSE1407"]},
    "geo_sample":    {"fp": [],  "tp": ["GSM10917","GSM1654"]},
    "pdb":           {"fp": [],  "tp": ["1CPC","2CHS","3ERE"]},
    "ena":           {"fp": [],  "tp": ["AF011567","AY057379","AB020640"]},
}


def self_test(verbose=True):
    """観測21件のFPを全件棄却、TP損失ゼロを検証。戻り値: 失敗件数(0で合格)。"""
    fp_total = fp_rejected = 0
    tp_total = tp_kept = 0
    leaks, losses = [], []
    for db, obs in OBSERVED.items():
        for s in obs["fp"]:
            fp_total += 1
            passed, reason = passes_gate(db, s)
            # soft(pdb 等)は「棄却しない」ことが仕様なので FP 判定対象から除外する必要はある。
            if passed:
                # soft-needs-downstream は「この層では通す」= 想定通り。ただし観測FPは
                # 全て hard db 由来なので、通過してしまえばリーク。
                leaks.append((db, s, reason)); 
            else:
                fp_rejected += 1
        for s in obs["tp"]:
            tp_total += 1
            passed, reason = passes_gate(db, s)
            if passed:
                tp_kept += 1
            else:
                losses.append((db, s, reason))

    if verbose:
        print("=== 桁数下限ゲート 自己検証(監査グラウンドトゥルース)===\n")
        print(f"観測FP: {fp_total} 件中 {fp_rejected} 件棄却"
              f"{' … 全件棄却 ✓' if fp_rejected==fp_total else ''}")
        if leaks:
            print("  !! 通過してしまったFP(リーク):")
            for db,s,r in leaks: print(f"     {db:<16}{s:<12}{r}")
        print(f"真陽性: {tp_total} 件中 {tp_kept} 件保持"
              f"{' … 損失ゼロ ✓' if tp_kept==tp_total else ''}")
        if losses:
            print("  !! 棄却してしまったTP(損失):")
            for db,s,r in losses: print(f"     {db:<16}{s:<12}{r}")
        print()
        print("db別の判定(FP=棄却が期待 / TP=通過が期待):")
        for db, obs in OBSERVED.items():
            spec = FLOOR_SPEC.get(db, {})
            print(f"\n--- {db}  [{spec.get('kind','?')}, floor={spec.get('floor')}]  {local_pattern(db)}")
            for s in obs["fp"]:
                p,r = passes_gate(db,s)
                print(f"    FP {s:<12} -> {'!! 通過' if p else 'OK 棄却':<8} ({r})")
            for s in obs["tp"]:
                p,r = passes_gate(db,s)
                print(f"    TP {s:<12} -> {'OK 通過' if p else '!! 棄却':<8} ({r})")

    fails = len(leaks) + len(losses)
    if verbose:
        print(f"\n{'合格: 観測FP全棄却 & TP損失ゼロ' if fails==0 else f'不合格: {fails} 件'}")
    return fails


# ─────────────────────────────────────────────────────────────────────────────
# parquet 後付け適用
# ─────────────────────────────────────────────────────────────────────────────
def _apply_to_parquet(indir, outdir=None, drop=False, report_only=False):
    import pandas as pd
    files = sorted(glob.glob(os.path.join(indir, "*.parquet"))) if os.path.isdir(indir) else [indir]
    if not files:
        print(f"parquet が見つからない: {indir}", file=sys.stderr); return
    if outdir and not report_only:
        os.makedirs(outdir, exist_ok=True)

    tot = Counter(); rej = Counter(); reasons = Counter()
    rej_samples = {}
    for i, f in enumerate(files, 1):
        df = pd.read_parquet(f)
        # ローカルID: identifier(CURIE)から接頭辞除去。無ければ surface で代替。
        src = df["identifier"] if "identifier" in df.columns else df["surface"]
        local = src.map(strip_prefix)
        res = [passes_gate(db, lid) for db, lid in zip(df["db"], local)]
        gate_pass = [r[0] for r in res]
        gate_reason = [r[1] for r in res]
        df["gate_pass"] = gate_pass
        df["gate_reason"] = gate_reason

        for db, gp, gr, lid in zip(df["db"], gate_pass, gate_reason, local):
            tot[db] += 1
            if not gp:
                rej[db] += 1; reasons[(db, gr)] += 1
                rej_samples.setdefault(db, [])
                if len(rej_samples[db]) < 8 and lid not in rej_samples[db]:
                    rej_samples[db].append(lid)

        if not report_only:
            out_df = df[df["gate_pass"]] if drop else df
            out_path = os.path.join(outdir, os.path.basename(f)) if outdir else f
            out_df.to_parquet(out_path, index=False)
        if i % 50 == 0 or i == len(files):
            print(f"  {i}/{len(files)} shards", flush=True)

    print("\n=== db別 桁数下限ゲート適用結果 ===")
    print(f"{'db':<20}{'total':>10}{'rejected':>10}{'kept':>10}{'rej%':>8}  kind")
    grand_t = grand_r = 0
    for db in sorted(tot, key=lambda k: -rej[k]):
        t, r = tot[db], rej[db]
        grand_t += t; grand_r += r
        kind = FLOOR_SPEC.get(db, {}).get("kind", "no-rule")
        print(f"{db:<20}{t:>10,}{r:>10,}{t-r:>10,}{(r/t*100 if t else 0):>7.1f}%  {kind}")
    print(f"{'─'*66}")
    print(f"{'TOTAL':<20}{grand_t:>10,}{grand_r:>10,}{grand_t-grand_r:>10,}"
          f"{(grand_r/grand_t*100 if grand_t else 0):>7.1f}%")

    if rej:
        print("\n=== 棄却された実文字列サンプル(db別)===")
        for db in sorted(rej, key=lambda k: -rej[k]):
            print(f"  {db:<20} {rej_samples.get(db, [])}")
    if report_only:
        print("\n(report-only: parquet は書き出していない)")
    elif outdir:
        print(f"\n書き出し先: {outdir}  ({'通過行のみ' if drop else 'gate_pass/gate_reason 付与・全行'})")
    else:
        print(f"\n※ --out 未指定のため入力を上書き更新した({'通過行のみ' if drop else '列付与'})")


def main():
    ap = argparse.ArgumentParser(description="桁数下限ゲート(3層ゲート第1層)")
    ap.add_argument("indir", nargs="?", help="parquet ディレクトリ or 単一ファイル")
    ap.add_argument("--out", help="書き出し先ディレクトリ(未指定なら入力を上書き)")
    ap.add_argument("--drop", action="store_true", help="通過行のみ残す(既定は列付与で全行保持)")
    ap.add_argument("--report-only", action="store_true", help="書き出さず集計のみ")
    ap.add_argument("--self-test", action="store_true", help="監査グラウンドトゥルースで検証")
    a = ap.parse_args()

    if a.self_test or not a.indir:
        fails = self_test()
        sys.exit(1 if fails else 0)
    _apply_to_parquet(a.indir, a.out, a.drop, a.report_only)


if __name__ == "__main__":
    main()
