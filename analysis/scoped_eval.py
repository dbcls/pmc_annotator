#!/usr/bin/env python3
"""寄託データ引用(主指標) / 参照リソース(副次指標) に分けた precision-recall。
  python scoped_eval.py eupmc.jsonl accession_annotations_togoid PMC-ids.csv.gz --strip-version
"""
import sys, json, re, os, glob, argparse
from collections import defaultdict, Counter
import pandas as pd

ap=argparse.ArgumentParser()
ap.add_argument("eupmc"); ap.add_argument("mine"); ap.add_argument("pmids")
ap.add_argument("--strip-version", action="store_true")
a=ap.parse_args()

# ---- eupmc repository -> 群 ----
def eu_group(repo_title):
    t = (repo_title or "").lower()
    DEPOSIT = ["european nucleotide archive","protein data bank","bioproject","biosample",
               "gene expression omnibus","arrayexpress","pride","dbgap","genotypes and phenotypes",
               "sequence read archive","electron microscopy data bank","genome assembly",
               "international genome sample resource"]
    REFERENCE = ["uniprot","reference sequence","ensembl","pfam","interpro","flybase"]
    for k in DEPOSIT:
        if k in t: return "deposit"
    for k in REFERENCE:
        if k in t: return "reference"
    return "other"

# ---- mine db -> 群 ----
MINE_DEPOSIT = {"geo_series","geo_sample","sra_accession","sra_project","sra_experiment",
                "sra_run","sra_sample","sra_analysis","bioproject","biosample",
                "arrayexpress","pride","assembly_insdc","nbdc_human_db","togovar"}
MINE_REFERENCE = {"uniprot","uniprot_isoform","uniparc","refseq_rna","refseq_protein",
                  "refseq_genomic","ensembl_protein","ensembl_transcript","pfam","interpro",
                  "flybase_transcript","flybase_protein"}
# 注: flybase_gene は概念/エンティティ言及(対象外)。ensembl_gene 同様どの群にも
#     入れず、mine_group で "concept" に落ちて集計から除外される。
MINE_EXCLUDE = {"dbsnp"}   # 偽陽性(rs1/rs2)
def mine_group(db):
    if db in MINE_EXCLUDE: return "excluded"
    if db in MINE_DEPOSIT: return "deposit"
    if db in MINE_REFERENCE: return "reference"
    return "concept"

def norm_doi(x):
    if x is None: return None
    x=str(x).strip().lower(); return re.sub(r'^https?://(dx\.)?doi\.org/','',x) or None
def norm_acc(x): return (str(x).strip().upper() or None) if x is not None else None
def sv(s): return s.split(".")[0] if s else s
def strip_prefix(x):
    if x is None: return None
    x=str(x); return x.split(":",1)[1] if ":" in x else x

# eupmc: 群別ペア + 論文集合
eu_pairs = {"deposit":set(), "reference":set(), "other":set()}
eu_docs=set(); eu_by_doc=defaultdict(lambda: defaultdict(int))
for line in open(a.eupmc):
    r=json.loads(line)
    ds=norm_acc(r.get("dataset")); ds=sv(ds) if a.strip_version else ds
    pub=norm_doi(r.get("publication"))
    if pub: eu_docs.add(pub)
    if not (ds and pub): continue
    rep=r.get("repository") or {}
    title=rep.get("title") if isinstance(rep,dict) else str(rep)
    g=eu_group(title)
    eu_pairs[g].add((ds,pub)); eu_by_doc[pub][g]+=1

cw=pd.read_csv(a.pmids,dtype=str,usecols=lambda c:c in {"PMID","DOI"},low_memory=False)
pmid2doi=dict(zip(cw["PMID"].fillna(""), cw["DOI"].map(norm_doi)))

files=sorted(glob.glob(os.path.join(a.mine,"*.parquet"))) if os.path.isdir(a.mine) else [a.mine]
mine_pairs=defaultdict(set); mine_docs=set(); seen=set()
for i,f in enumerate(files,1):
    df=pd.read_parquet(f, columns=["identifier","pmid","db"])
    for idv,pmid,db in zip(df["identifier"],df["pmid"],df["db"]):
        doi=pmid2doi.get(str(pmid).strip())
        if not doi: continue
        mine_docs.add(doi)
        ds=norm_acc(strip_prefix(idv)); ds=sv(ds) if a.strip_version else ds
        if not ds: continue
        g=mine_group(db)
        if g in ("excluded","concept"): continue
        k=(g,ds,doi)
        if k in seen: continue
        seen.add(k); mine_pairs[g].add((ds,doi))
    if i%20==0 or i==len(files): print(f"  {i}/{len(files)} shards",flush=True)

ov=eu_docs & mine_docs
print(f"\n文書オーバーラップ: {len(ov):,}\n")
print(f"{'群':<12}{'eupmc(全)':>12}{'eupmc(重なり内)':>16}{'mine':>10}{'both':>8}{'prec%':>8}{'rec%':>8}")
for g in ("deposit","reference"):
    eup=eu_pairs[g]
    eup_ov={(d,p) for d,p in eup if p in ov}          # 公平な分母
    m={(d,p) for d,p in mine_pairs[g] if p in ov}
    b=m & eup_ov
    prec=(len(b)/len(m)*100) if m else 0
    rec =(len(b)/len(eup_ov)*100) if eup_ov else 0
    print(f"{g:<12}{len(eup):>12,}{len(eup_ov):>16,}{len(m):>10,}{len(b):>8,}{prec:>7.1f}%{rec:>7.1f}%")

# 主指標の分母内訳(何が取れていないか)
print("\n[deposit] eupmc重なり内の未一致ペア repository内訳 top10:")
rep_of=defaultdict(str)
for line in open(a.eupmc):
    r=json.loads(line)
    ds=norm_acc(r.get("dataset")); ds=sv(ds) if a.strip_version else ds
    pub=norm_doi(r.get("publication"))
    rep=r.get("repository") or {}
    t=rep.get("title") if isinstance(rep,dict) else str(rep)
    if ds and pub and eu_group(t)=="deposit": rep_of[(ds,pub)]=t or "(none)"
eup_ov={(d,p) for d,p in eu_pairs["deposit"] if p in ov}
missed=eup_ov - {(d,p) for d,p in mine_pairs["deposit"] if p in ov}
for name,c in Counter(rep_of[k] for k in missed).most_common(10):
    print(f"  {c:>8,}  {name}")
