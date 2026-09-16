#!/usr/bin/env python3
"""T3: NCBI eutils / ENA による実在性検証 (最小版)。
verify_t2 の verified.tsv の status=pending 行のうち、T3_ROUTE 登録dbを確定する。
T3は権威DBなので absent を主張できる。未登録dbは pending 据置。

対象 (この版):
  refseq_protein -> efetch db=protein rettype=acc   (版無視のprefix一致)
  refseq_rna     -> efetch db=nuccore rettype=acc
  refseq_genomic -> efetch db=nuccore rettype=acc
  geo_series     -> esearch db=gds "GSE…[ACCN]" Count>0
  geo_sample     -> esearch db=gds "GSM…[ACCN]" Count>0
  insdc / insdc_master -> ENA browser API (embl flatfile HEAD; 200/404 is real,
      unlike identifiers.org's insdc redirect target, ENA's JS browser front-end,
      which returns 200 regardless of whether the accession exists)
未登録(affy_probeset/ensembl系/tair/… )は pending 据置。

  export NCBI_API_KEY=xxxx   # あれば 10 req/s
  python3 verify_t3.py route --in verified.tsv         # 対象db・件数の確認(API叩かない)
  python3 verify_t3.py run   --in verified.tsv --out verified.t3.tsv [--inplace] [--workers 8]
"""
import sys, os, re, time, json, sqlite3, argparse, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

EUTILS="https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ENA_API="https://www.ebi.ac.uk/ena/browser/api/embl/{id}"
API_KEY=os.environ.get("NCBI_API_KEY")
EMAIL=os.environ.get("NCBI_EMAIL")
TOOL="pmc-t3-verify"
CACHE="t3_cache.sqlite"

# db -> (方式, ncbi_db)
T3_ROUTE={
    "refseq_protein": ("refseq","protein"),
    "refseq_rna":     ("refseq","nuccore"),
    "refseq_genomic": ("refseq","nuccore"),
    "geo_series":     ("geo","gds"),
    "geo_sample":     ("geo","gds"),
    "insdc":          ("ena", None),
    "insdc_master":   ("ena", None),
}
def strip_ver(a): return re.sub(r'\.\d+$','',a.strip())

def _params(extra):
    p=dict(tool=TOOL); 
    if API_KEY: p["api_key"]=API_KEY
    if EMAIL:   p["email"]=EMAIL
    p.update(extra); return p

def eutils(endpoint, extra, post=False, timeout=60, retry=4):
    url=f"{EUTILS}/{endpoint}.fcgi"
    data=urllib.parse.urlencode(_params(extra)).encode()
    for k in range(retry):
        try:
            if post:
                req=urllib.request.Request(url, data=data,
                    headers={"User-Agent":TOOL})
            else:
                req=urllib.request.Request(url+"?"+data.decode(),
                    headers={"User-Agent":TOOL})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8","replace"), 200
        except urllib.error.HTTPError as e:
            if e.code in (400,414) and not post:   # URL長すぎ等 -> POST再試行はしない、コードだけ返す
                return "", e.code
            if e.code in (429,500,502,503):
                time.sleep(1.5*(k+1)); continue
            return "", e.code
        except Exception:
            time.sleep(1.0*(k+1)); continue
    return "", -1

# ---- refseq: efetch rettype=acc をバッチ、400はバイセクションで不正ID特定 ----
def efetch_found(ncbi_db, ids, sleep):
    """ids のうち実在する版無しaccession集合を返す。"""
    if not ids: return set()
    body, code = eutils("efetch",
        dict(db=ncbi_db, id=",".join(ids), rettype="acc", retmode="text"), post=True)
    time.sleep(sleep)
    if code==200:
        found=set()
        for ln in body.splitlines():
            t=ln.strip()
            if t and re.match(r'^[A-Za-z]{1,3}_?\d', t) and not t.lower().startswith("error"):
                found.add(strip_ver(t))
        return found
    # 400等: 単一なら不在、複数なら二分探索
    if len(ids)==1: return set()
    mid=len(ids)//2
    return efetch_found(ncbi_db, ids[:mid], sleep) | efetch_found(ncbi_db, ids[mid:], sleep)

def verify_refseq(ncbi_db, ids, cache, batch, sleep):
    todo=[i for i in ids if i not in cache]
    for k in range(0,len(todo),batch):
        chunk=todo[k:k+batch]
        found=efetch_found(ncbi_db, chunk, sleep)
        for i in chunk:
            cache[i] = "confirmed" if strip_ver(i) in found else "absent"
    return {i:cache[i] for i in ids}

# ---- geo: esearch [ACCN] の Count ----
def verify_geo(ids, cache, sleep):
    for i in ids:
        if i in cache: continue
        body,code=eutils("esearch", dict(db="gds", term=f"{i}[ACCN]", retmode="json"))
        ok=False
        if code==200:
            try: ok=int(json.loads(body)["esearchresult"]["count"])>0
            except Exception: ok=False
        cache[i]="confirmed" if ok else ("absent" if code==200 else "error")
        time.sleep(sleep)
    return {i:cache[i] for i in ids}

# ---- ena: real per-record data API (HEAD), not the JS browser front-end ----
def _ena_lookup(acc, sleep):
    req=urllib.request.Request(ENA_API.format(id=urllib.parse.quote(acc)), method="HEAD",
                               headers={"User-Agent":TOOL})
    try:
        with urllib.request.urlopen(req, timeout=30):
            status="confirmed"
    except urllib.error.HTTPError as e:
        status="absent" if e.code==404 else "error"
    except Exception:
        status="error"
    time.sleep(sleep)
    return acc, status

def verify_ena(ids, cache, sleep, workers):
    todo=[i for i in ids if i not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(_ena_lookup, i, sleep) for i in todo]
            for fut in as_completed(futures):
                acc, status = fut.result()
                cache[acc] = status
    return {i:cache[i] for i in ids}

# ---- cache ----
def open_cache():
    c=sqlite3.connect(CACHE)
    c.execute("CREATE TABLE IF NOT EXISTS t3(db TEXT,id TEXT,status TEXT,PRIMARY KEY(db,id))")
    return c
def cache_load(c, db): return {r[0]:r[1] for r in c.execute("SELECT id,status FROM t3 WHERE db=?", (db,))}
def cache_save(c, db, d):
    c.executemany("INSERT OR REPLACE INTO t3 VALUES(?,?,?)", [(db,i,s) for i,s in d.items()]); c.commit()

# ---- verified.tsv 読み書き ----
def read_verified(path):
    rows=[]; header=None
    for ln in open(path, encoding="utf-8"):
        ln=ln.rstrip("\n")
        if not ln: continue
        if header is None and ln.split("\t")[0]=="dataset":
            header=ln; continue
        rows.append(ln.split("\t"))
    return header, rows   # rows: [dataset,id,status,oracle]

def cmd_route(a):
    _,rows=read_verified(a.__dict__["in"])
    from collections import Counter
    pend=Counter(r[0] for r in rows if len(r)>2 and r[2]=="pending")
    tgt=sum(v for k,v in pend.items() if k in T3_ROUTE)
    print(f"pending 総db種={len(pend)}  総件数={sum(pend.values()):,}")
    print(f"T3対象(登録db)= {tgt:,} 件\n")
    print("db別 (★=T3対象):")
    for db,n in pend.most_common():
        route = f"{T3_ROUTE[db][0]}/{T3_ROUTE[db][1]}" if db in T3_ROUTE and T3_ROUTE[db][1] else \
                (T3_ROUTE[db][0] if db in T3_ROUTE else None)
        print(f"  {'★' if db in T3_ROUTE else ' '} {db:18s} {n:>7,d}"
              + (f"  -> {route}" if route else "  (pending据置)"))

def cmd_run(a):
    header,rows=read_verified(a.__dict__["in"])
    from collections import defaultdict
    pend=defaultdict(set)
    for r in rows:
        if len(r)>2 and r[2]=="pending" and r[0] in T3_ROUTE:
            pend[r[0]].add(r[1])
    c=open_cache(); result={}; db_oracle={}   # (db,id)->status, db->oracle label
    for db,ids in pend.items():
        method,ncbi=T3_ROUTE[db]
        cache=cache_load(c, db)
        ids=sorted(ids)
        if method=="refseq":
            res=verify_refseq(ncbi, ids, cache, a.batch, a.sleep)
            db_oracle[db]="ncbi_eutils"
        elif method=="geo":
            res=verify_geo(ids, cache, a.sleep)
            db_oracle[db]="ncbi_eutils"
        else:
            res=verify_ena(ids, cache, a.sleep, a.workers)
            db_oracle[db]="ena_api"
        cache_save(c, db, cache)
        for i,s in res.items(): result[(db,i)]=s
        nc=sum(1 for s in res.values() if s=="confirmed")
        print(f"{db:18s} {len(ids):>6,d} 件 -> confirmed={nc:,} absent={sum(1 for s in res.values() if s=='absent'):,} error={sum(1 for s in res.values() if s=='error'):,}", flush=True)

    out=a.out if not a.inplace else a.__dict__["in"]
    with open(out,"w",encoding="utf-8") as w:
        w.write((header or "dataset\tid\tstatus\toracle")+"\n")
        conf=0
        for r in rows:
            while len(r)<4: r.append("-")
            ds,i,st,orc=r[0],r[1],r[2],r[3]
            if st=="pending" and (ds,i) in result:
                s=result[(ds,i)]
                if s in ("confirmed","absent"):
                    st=s; orc=db_oracle.get(ds,"ncbi_eutils"); conf+=(s=="confirmed")
            w.write(f"{ds}\t{i}\t{st}\t{orc}\n")
    print(f"\nT3で確定(confirmed) 追加= {conf:,}  -> {out}")

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    for name in ("route","run"):
        p=sub.add_parser(name); p.add_argument("--in",required=True)
        if name=="run":
            p.add_argument("--out",default="verified.t3.tsv")
            p.add_argument("--inplace",action="store_true")
            p.add_argument("--batch",type=int,default=200)
            p.add_argument("--sleep",type=float,default=(0.11 if API_KEY else 0.34))
            p.add_argument("--workers",type=int,default=8,help="Concurrent threads for the ena route")
        p.set_defaults(func={"route":cmd_route,"run":cmd_run}[name])
    a=ap.parse_args(); a.func(a)

if __name__=="__main__": main()
