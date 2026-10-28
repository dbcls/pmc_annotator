#!/usr/bin/env python3
"""
sample_heldout.py — held-out 注釈シートの層化抽出（ブラインド）

held-out は dev gold と「ドキュメント単位」で素にする（既定）。抽出元は既存の
windows_all.jsonl（役割分類と同じプール）でよく、再抽出・再 build_windows は不要。

入力:
  --windows        windows_all.jsonl（role_prepass と同じスキーマ）
  --prepass        同プールに role_prepass.py をかけた role_prelim.tsv
                   ※層化（クラス下限の底上げ）にのみ使用。出力には載せない
  --exclude-gold   dev の gold_standard.md
  --exclude-level  doc(既定)=gold に出る doc_id を丸ごと除外 / key=(doc,dataset,id) のみ除外
出力:
  --out  空の注釈シート(.md, gold と同じ列, gold_role 等は空欄)
  --key  sample_id→(doc,dataset,id,layer) 対応（設計者QC用。注釈者には渡さない）
  --iaa  二重注釈用 sample_id リスト
"""
from __future__ import annotations
import argparse, json, re, random, sys
from collections import defaultdict, Counter

GOLD_COLS = ["sample_id","doc_id","dataset","id","class","section_type",
             "window_text","gold_role","gold_subtag","gold_note"]

def parse_gold(path):
    keys=set(); docs=set()
    for line in open(path, encoding="utf-8"):
        if not line.lstrip().startswith("|"): continue
        c=[x.strip() for x in line.rstrip("\n").split("|")[1:-1]]
        if len(c)<4 or c[0].lower()=="sample_id": continue
        if all(re.match(r'^:?-+:?$',x) for x in c if x): continue
        keys.add((c[1],c[2],c[3])); docs.add(c[1])
    return keys, docs

def load_prepass(path):
    bucket={}
    with open(path, encoding="utf-8") as f:
        header=f.readline().rstrip("\n").split("\t")
        idx={h:i for i,h in enumerate(header)}
        for line in f:
            c=line.rstrip("\n").split("\t")
            if len(c)<len(header): continue
            key=(c[idx["doc_id"]],c[idx["dataset"]],c[idx["id"]])
            role=(c[idx["role"]] or "").strip(); prior=(c[idx["prior"]] or "").strip()
            if role in ("used","mentioned"): b=role
            elif prior=="generated":         b="generated"
            elif prior in ("used","use"):     b="used"
            else:                             b="mentioned"
            bucket[key]=b
    return bucket

def pick_windows(path):
    best={}
    for line in open(path, encoding="utf-8"):
        line=line.strip()
        if not line: continue
        r=json.loads(line)
        if not r.get("matched"): continue
        key=(r["doc_id"],r["dataset"],r["id"])
        w=r.get("window_text","") or ""
        score=(1 if "«" in w else 0, len(w))
        cur=best.get(key)
        if cur is None or score>cur[0]: best[key]=(score, r)
    return {k:v[1] for k,v in best.items()}

def cell(s): return re.sub(r"\s+"," ", (s or "")).replace("|","\\|").strip()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--windows", required=True)
    ap.add_argument("--prepass", required=True)
    ap.add_argument("--exclude-gold", required=True)
    ap.add_argument("--exclude-level", choices=["doc","key"], default="doc")
    ap.add_argument("--per-class", default="generated=80,used=100,mentioned=100")
    ap.add_argument("--dataset-cap", default="", help="DB別 上限。0 は除外。例 refseq_genomic=25,cog=0")
    ap.add_argument("--dataset-floor", default="", help="DB別 下限。例 mirbase=15,chebi=10")
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--iaa-frac", type=float, default=0.18)
    ap.add_argument("--prefix", default="H")
    ap.add_argument("--out", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--iaa", required=True)
    args=ap.parse_args()

    target={k:int(v) for k,v in (p.split("=") for p in args.per_class.split(","))}
    dcap={k:int(v) for k,v in (p.split("=") for p in args.dataset_cap.split(",") if p)} if args.dataset_cap else {}
    dfloor={k:int(v) for k,v in (p.split("=") for p in args.dataset_floor.split(",") if p)} if args.dataset_floor else {}
    hard_excl={ds for ds,c in dcap.items() if c==0}
    rng=random.Random(args.seed)

    gkeys, gdocs = parse_gold(args.exclude_gold)
    bucket=load_prepass(args.prepass)
    wins=pick_windows(args.windows)

    def excluded(key):
        return (key[0] in gdocs) if args.exclude_level=="doc" else (key in gkeys)

    cand=defaultdict(list); n_excl=0; n_hard=0
    for key in wins:
        if excluded(key): n_excl+=1; continue
        if key[1] in hard_excl: n_hard+=1; continue
        cand[bucket.get(key,"mentioned")].append(key)

    chosen=[]; used_per_ds=defaultdict(int); taken=set(); rem=dict(target)
    # Phase1: DB別フロアを先に満たす（クラス枠を消費）
    for ds, fl in dfloor.items():
        items=[(b,key) for b in rem for key in cand.get(b,[]) if key[1]==ds and key not in taken]
        rng.shuffle(items)
        for b,key in items:
            if used_per_ds[ds]>=fl: break
            if rem[b]<=0: continue
            if ds in dcap and used_per_ds[ds]>=dcap[ds]: break
            chosen.append((b,key)); taken.add(key); used_per_ds[ds]+=1; rem[b]-=1
        if used_per_ds[ds]<fl:
            print(f"[warn] floor {ds}: 目標{fl} に対し {used_per_ds[ds]} 件のみ（pool不足）", file=sys.stderr)
    # Phase2: クラス枠を埋める（ソフトキャップ, 不足時のみ超過許容）
    for b, want in target.items():
        pool=[k for k in cand.get(b,[]) if k not in taken]; rng.shuffle(pool)
        deferred=[]
        for key in pool:
            if rem[b]<=0: break
            ds=key[1]
            if ds in dcap and used_per_ds[ds]>=dcap[ds]: deferred.append(key); continue
            chosen.append((b,key)); taken.add(key); used_per_ds[ds]+=1; rem[b]-=1
        for key in deferred:
            if rem[b]<=0: break
            chosen.append((b,key)); taken.add(key); used_per_ds[key[1]]+=1; rem[b]-=1
        if rem[b]>0:
            print(f"[warn] bucket={b}: 目標{want} に {want-rem[b]} 件のみ（候補不足）", file=sys.stderr)

    rng.shuffle(chosen)
    n=len(chosen); ids=[f"{args.prefix}{i+1:04d}" for i in range(n)]
    iaa=set(rng.sample(ids, max(1,int(round(n*args.iaa_frac)))))

    with open(args.out,"w",encoding="utf-8") as fo, open(args.key,"w",encoding="utf-8") as fk:
        fo.write("| "+" | ".join(GOLD_COLS)+" |\n"); fo.write("|"+"|".join(["---"]*len(GOLD_COLS))+"|\n")
        fk.write("sample_id\tdoc_id\tdataset\tid\tprepass_layer\n")
        for sid,(b,key) in zip(ids, chosen):
            doc,ds,eid=key; rec=wins[key]
            row=[sid,doc,ds,eid,cell(rec.get("class","")),cell(rec.get("section_type","")),
                 cell(rec.get("window_text","")),"","",""]
            fo.write("| "+" | ".join(row)+" |\n"); fk.write(f"{sid}\t{doc}\t{ds}\t{eid}\t{b}\n")
    with open(args.iaa,"w",encoding="utf-8") as fi:
        for sid in sorted(iaa): fi.write(sid+"\n")

    byb=Counter(b for b,_ in chosen); byd=Counter(k[1] for _,k in chosen)
    print(f"[ok] 抽出 {n} 件 -> {args.out}", file=sys.stderr)
    print(f"  除外 ({args.exclude_level} 単位, gold doc={len(gdocs)} / key={len(gkeys)}): gold {n_excl} 件 + hard-cap(0) {n_hard} 件", file=sys.stderr)
    print(f"  層(prepass proxy): {dict(byb)}", file=sys.stderr)
    print(f"  dataset: {dict(byd)}", file=sys.stderr)
    print(f"  IAA 二重注釈: {len(iaa)} 件 -> {args.iaa}", file=sys.stderr)

if __name__=="__main__": main()
