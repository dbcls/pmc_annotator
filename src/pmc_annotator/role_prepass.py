"""
role_prepass.py — 役割判定 部品(a): ルール前段 + クラスルーター  [LLM なし]

windows.jsonl を入力に、(doc,dataset,id) 単位へ集約して:
- out_of_scope        : スキップ
- derived/curated     : 2値(使用/言及)をルールで即決、稀な曖昧のみ LLM 送り
- deposit 系          : 即決せず prior(gen/use/none)を付けて全件 LLM 送り
を行い role_prelim.tsv を出力。即決/prior の内訳を stderr に集計表示。
"""
from __future__ import annotations
import argparse, csv, json, re, sys
from collections import defaultdict, Counter

# --- 明示寄託(生成) ---
GEN_DEP = re.compile(
    r"\b("
    r"deposit(ed|ing)?|were deposited|has been deposited|have been (deposited|submitted)"
    r"|submitt(ed|ing)|we (deposited|submitted|generated|sequenced|produced|created)"
    r"|were (generated|produced|created)|assigned the accession"
    r"|the following (data|datasets?|sequences?|samples?) (were|are)"
    r")\b", re.I)

# --- 明示取得(使用/再利用)。reanalysis 名詞形を含む ---
USE_ACQ = re.compile(
    r"\b("
    r"download(ed|ing)?|obtained from|retrieved from|acquired from|taken from|sourced from"
    r"|re-?analys[ie]s|re-?analyz(ed|ing|es|e)"
    r"|we (used|obtained|retrieved|downloaded|acquired)"
    r"|datasets? (were|was) (obtained|downloaded|retrieved|used)"
    r"|were (obtained|downloaded|retrieved)"
    r")\b", re.I)

# --- 曖昧な availability(prior を立てない=LLM 判断) ---
AVAIL = re.compile(r"\b(publicly|freely) available\b|\bavailable (at|from|in|under|through)\b", re.I)

# --- 参照資源としての利用(derived で 使用 昇格): bare blast/query を外し高精度に限定 ---
# 追加 trim: 'alignment with'(設定のwith) と against/using the …database を除去
REF_USE = re.compile(
    r"\b("
    r"aligned (to|against)|align(ing|ment) (to|against)|mapp(ed|ing) (to|against|onto)"
    r"|using the [\w\s\-]{0,25}(genome|assembly|reference)"
    r"|reference (genome|sequence|assembly|proteome)"
    r"|index(ed)? (to|against)|against the [\w\s\-]{0,25}(genome|assembly)"
    r"|as (the )?quer(y|ies)"
    r"|ref\|"                                   # NCBI/BLAST defline  ref|XP_…|
    r"|orthologs?\b[^.]{0,40}(aligned|from)"    # ortholog alignment listings
    r"|aligned in (clustal|muscle|mafft|t-?coffee)"
    r"|compared (to|with) [^.]{0,40}(accession|genbank|refseq|reference sequence)"
    r")\b", re.I)

# --- ドメイン/ファミリー registry: ほぼ常に「注釈=言及」。used 昇格させない ---
DOMAIN_FAMILY_DBS = {
    "cog", "pfam", "interpro", "cdd", "smart", "tigrfam", "tigrfams",
    "panther", "prosite", "superfamily", "prints", "pirsf", "hamap",
    "gene3d", "sfld", "ncbifam",
}

# --- Data Availability / Accession 節の見出し(deposit で gen prior 強化) ---
DAS = re.compile(
    r"\b(data availability|availability of (the )?(data|supporting data)"
    r"|accession (numbers?|codes?)|data deposition|data access)\b", re.I)

DEPOSIT_CLASSES = {"deposited_research_record", "deposited_entity_registry"}
REF_CLASSES = {"derived_curated", "curated_entity_registry"}


def signals(text):
    return {
        "gen_dep": bool(GEN_DEP.search(text)),
        "use_acq": bool(USE_ACQ.search(text)),
        "avail":   bool(AVAIL.search(text)),
        "ref_use": bool(REF_USE.search(text)),
        "das":     bool(DAS.search(text)),
    }


def route(cls, dataset, sig, flat_table):
    """(role, prior, role_source, to_llm, note) を返す。"""
    if cls == "out_of_scope":
        return "", "", "skip", False, "out_of_scope"

    if cls in REF_CLASSES:
        if (dataset or "").lower() in DOMAIN_FAMILY_DBS:
            return "mentioned", "", "rule", False, "domain_family"
        if flat_table:
            return "mentioned", "", "rule", False, "flat_table"
        if sig["ref_use"]:
            return "used", "", "rule", False, "ref_resource_use"
        if sig["use_acq"]:
            return "", "use", "llm_pending", True, "ref_reuse_ambiguous"
        return "", "none", "llm_pending", True, "default_to_llm"

    if cls in DEPOSIT_CLASSES:
        if sig["use_acq"] and sig["gen_dep"]:
            prior, note = "none", "gen_use_conflict"
        elif sig["gen_dep"] or (sig["das"] and not sig["use_acq"]):
            prior, note = "generated", ("gen_dep" if sig["gen_dep"] else "das_backed")
        elif sig["use_acq"]:
            prior, note = "used", "use_acq"
        elif sig["avail"]:
            prior, note = "none", "avail_ambiguous"
        else:
            prior, note = "none", "no_cue"
        return "", prior, "llm_pending", True, note

    return "", "none", "llm_pending", True, f"unknown_class:{cls}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", required=True, help="build_windows の windows.jsonl")
    ap.add_argument("--out", required=True, help="出力 role_prelim.tsv")
    args = ap.parse_args()

    # (doc,dataset,id) へ集約
    groups = defaultdict(lambda: {"class": None, "section_type": None,
                                  "flat_table": False, "n_win": 0, "matched": False,
                                  "sig": {k: False for k in ("gen_dep", "use_acq", "avail", "ref_use", "das")},
                                  "n_in_doc": None})
    n_lines = 0
    with open(args.windows) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            r = json.loads(line)
            key = (r["doc_id"], r["dataset"], r["id"])
            g = groups[key]
            g["class"] = r.get("class")
            g["n_in_doc"] = r.get("n_in_doc")
            if not r.get("matched"):
                continue
            g["matched"] = True
            g["n_win"] += 1
            g["flat_table"] = g["flat_table"] or bool(r.get("flat_table"))
            # section_type は生成語を含む窓のを優先採用(案A的)
            s = signals(r.get("window_text", ""))
            take_section = (g["section_type"] is None) or (s["gen_dep"] and not g["sig"]["gen_dep"])
            if take_section:
                g["section_type"] = r.get("section_type")
            for k in s:
                g["sig"][k] = g["sig"][k] or s[k]

    cnt = Counter()
    per_class = defaultdict(Counter)
    llm_docs = set()

    with open(args.out, "w", newline="") as fo:
        w = csv.writer(fo, delimiter="\t")
        w.writerow(["doc_id", "dataset", "id", "class", "role", "prior",
                    "role_source", "to_llm", "section_type",
                    "gen_dep", "use_acq", "avail", "ref_use", "das", "flat_table",
                    "n_win", "n_in_doc", "note"])
        for (doc_id, dataset, ent_id), g in groups.items():
            cls = g["class"]
            if not g["matched"]:
                w.writerow([doc_id, dataset, ent_id, cls, "", "", "no_window", 0,
                            g["section_type"], 0, 0, 0, 0, 0, int(g["flat_table"]),
                            0, g["n_in_doc"], "no_window"])
                cnt["no_window"] += 1
                per_class[cls]["no_window"] += 1
                continue
            sig = g["sig"]
            role, prior, source, to_llm, note = route(cls, dataset, sig, g["flat_table"])
            w.writerow([doc_id, dataset, ent_id, cls, role, prior, source, int(to_llm),
                        g["section_type"],
                        int(sig["gen_dep"]), int(sig["use_acq"]), int(sig["avail"]),
                        int(sig["ref_use"]), int(sig["das"]), int(g["flat_table"]),
                        g["n_win"], g["n_in_doc"], note])
            cnt["entities"] += 1
            cnt["to_llm" if to_llm else "rule_final"] += 1
            if to_llm:
                llm_docs.add(doc_id)
            # 内訳
            if source == "rule":
                per_class[cls][f"rule:{role}"] += 1
            elif source == "llm_pending":
                per_class[cls][f"llm:prior={prior or 'none'}"] += 1
            elif source == "skip":
                per_class[cls]["skip"] += 1

    print(f"[info] 入力行={n_lines}  エンティティ={cnt['entities']}  "
          f"rule_final={cnt['rule_final']}  to_llm={cnt['to_llm']}  "
          f"no_window={cnt['no_window']}", file=sys.stderr)
    print(f"[info] LLM 対象 doc(=doc-level コール概算)={len(llm_docs)}", file=sys.stderr)
    print("[breakdown] class 別:", file=sys.stderr)
    for cls in sorted(per_class):
        items = "  ".join(f"{k}={v}" for k, v in sorted(per_class[cls].items()))
        print(f"  {cls}: {items}", file=sys.stderr)


if __name__ == "__main__":
    main()
