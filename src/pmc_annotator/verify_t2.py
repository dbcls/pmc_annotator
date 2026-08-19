#!/usr/bin/env python3
"""T2 実在性検証オーケストレータ。
  T2b = TogoID label graph (identifiers.org正規化・dcterm:identifier・raw ID一致) を第一オラクル
  T2a = rdf-config native graph (t2_registry.json) をフォールバック
存在 = T2b ∪ T2a、ミスは pending (T3送り)。absent は主張しない。
全SPARQLは https://rdfportal.org/backend/<group>/sparql を使用。
"""
import sys, os, re, json, time, argparse, urllib.request, urllib.parse

BACKEND = "https://rdfportal.org/backend/{group}/sparql"
LABEL_GRAPH = "http://rdfportal.org/dataset/togoid/label/{ds}"
DCT_ID = "http://purl.org/dc/terms/identifier"
TOGOID_YAML = "togoid_dataset.yaml"
REGISTRY   = "t2_registry.json"
COVERAGE   = "t2b_coverage.json"

def to_backend(ep):
    if not ep: return ep
    m=re.search(r'rdfportal\.org/(?:backend/)?([a-z0-9_]+)/sparql', ep)
    return BACKEND.format(group=m.group(1)) if m else ep

ROUTER = {
  "ensembl_gene":"ensembl","ensembl_protein":"ensembl","ensembl_transcript":"ensembl",
  "chembl_compound":"chembl","chembl_target":"chembl",
  "hp_phenotype":"hpo","hp_inheritance":"hpo","insdc":"ddbj","insdc_cds":"ddbj",
  "insdc_master":"ddbj","assembly_insdc":"ddbj","sra":"sra_experiment",
  "oma_group":"oma","oma_protein":"oma","hgnc_symbol":"hgnc",
  "mbgd_gene":"mbgd","mbgd_organism":"mbgd","omim_gene":"medgen",
}
def norm_id(db, i):
    if db in ("chebi","go","mondo","uberon","cl","clo","doid","mp","hpo","efo") or \
       re.match(r'^(CHEBI|GO|MONDO|HP|UBERON|CL|CLO|DOID|MP|EFO)[:_]', i):
        return re.sub(r'[:]', '_', i)
    return i

def sparql(endpoint, query, timeout=60):
    data=urllib.parse.urlencode({"query":query}).encode()
    req=urllib.request.Request(endpoint, data=data, headers={
        "Accept":"application/sparql-results+json",
        "Content-Type":"application/x-www-form-urlencoded",
        "User-Agent":"pmc-t2-verify/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8","replace"))

def ask(endpoint, query, timeout=60):
    return sparql(endpoint, query, timeout).get("boolean", False)

def load_togoid_datasets(path=TOGOID_YAML):
    import yaml
    d=yaml.safe_load(open(path, encoding="utf-8"))
    return [k for k,v in d.items() if isinstance(v,dict)]

def discover_coverage(datasets, sleep=0.05):
    ep=BACKEND.format(group="primary"); cov=[]
    for ds in datasets:
        g=LABEL_GRAPH.format(ds=ds)
        try:
            if ask(ep, f"ASK {{ GRAPH <{g}> {{ ?s ?p ?o }} }}"): cov.append(ds)
        except Exception as e:
            sys.stderr.write(f"[WARN] discover {ds}: {e}\n")
        time.sleep(sleep)
    return cov

def load_coverage():
    if os.path.exists(COVERAGE): return set(json.load(open(COVERAGE)))
    return None

def t2b_verify(ds, ids, cov, cache, batch=500, sleep=0.15):
    if ds not in cov: return {i:None for i in ids}
    ep=BACKEND.format(group="primary"); g=LABEL_GRAPH.format(ds=ds)
    todo=[i for i in ids if ("b",ds,i) not in cache]
    for k in range(0,len(todo),batch):
        chunk=todo[k:k+batch]
        values=" ".join('"'+i.replace('"','\\"')+'"' for i in chunk)
        q=(f"SELECT ?id FROM <{g}> WHERE {{ ?s <{DCT_ID}> ?id VALUES ?id {{ {values} }} }}")
        try:
            found={b["id"]["value"] for b in sparql(ep,q)["results"]["bindings"]}
        except Exception as e:
            sys.stderr.write(f"[WARN] T2b {ds}: {e}\n"); found=set()
        for i in chunk: cache[("b",ds,i)] = i in found
        time.sleep(sleep)
    return {i:cache.get(("b",ds,i)) for i in ids}

def load_registry(p=REGISTRY):
    reg={}
    for r in json.load(open(p)):
        r=dict(r); r["endpoint"]=to_backend(r.get("endpoint"))
        reg[r["db"]]=r
    return reg

def resolve_db(name, reg): return name if name in reg else ROUTER.get(name)

def build_query(row, uris, obj=False):
    frm=f"FROM <{row['graph']}>" if row.get("graph") else ""
    values=" ".join(f"<{u}>" for u in uris)
    pat="{ ?s ?p ?o } UNION { ?x ?p ?s }" if obj else "?s ?p ?o ."
    return f"SELECT DISTINCT ?s {frm} WHERE {{ VALUES ?s {{ {values} }} {pat} }}"

def t2a_verify(db, ids, reg, cache, batch=300, sleep=0.2, obj=False):
    row=reg.get(db)
    if not row or not (row.get("subj_uri_ns") and row.get("endpoint") and row.get("queryable")):
        return {i:None for i in ids}
    ns=row["subj_uri_ns"]; ep=row["endpoint"]
    todo=[i for i in ids if ("a",db,i) not in cache]
    for k in range(0,len(todo),batch):
        chunk=todo[k:k+batch]
        uri2id={ ns+norm_id(db,i): i for i in chunk }
        try:
            found={b["s"]["value"] for b in sparql(ep,build_query(row,list(uri2id),obj))["results"]["bindings"]}
        except Exception as e:
            sys.stderr.write(f"[WARN] T2a {db}: {e}\n"); found=set()
        for u,i in uri2id.items(): cache[("a",db,i)] = u in found
        time.sleep(sleep)
    return {i:cache.get(("a",db,i)) for i in ids}

def cmd_discover(a):
    ds=load_togoid_datasets(a.togoid)
    if not a.refresh and os.path.exists(COVERAGE):
        cov=sorted(load_coverage()); print(f"cached: {len(cov)} datasets ({COVERAGE})"); return
    cov=discover_coverage(ds)
    json.dump(sorted(cov), open(COVERAGE,"w"), ensure_ascii=False, indent=1)
    print(f"T2b label graph 有り: {len(cov)}/{len(ds)} dataset -> {COVERAGE}")
    print("例:", ", ".join(cov[:30]))

def cmd_smoke(a):
    reg=load_registry(a.registry); cov=load_coverage()
    if cov is None: print("先に discover を実行してください"); return
    ep=BACKEND.format(group="primary"); okb=0
    for ds in sorted(cov)[:a.limit]:
        g=LABEL_GRAPH.format(ds=ds)
        try:
            r=sparql(ep, f"SELECT ?id FROM <{g}> WHERE {{ ?s <{DCT_ID}> ?id }} LIMIT 1")
            hit=bool(r["results"]["bindings"]); okb+=hit
            print(f"{'OK ' if hit else 'NG '} T2b {ds}")
        except Exception as e: print(f"NG  T2b {ds}  ERR:{e}")
    oka=0; na=0
    for db,row in sorted(reg.items()):
        if not row.get("queryable") or not row.get("example"): continue
        na+=1; uri=row["subj_uri_ns"]+row["example"]
        try:
            r=sparql(row["endpoint"], build_query(row,[uri],a.object))
            hit=bool(r["results"]["bindings"]); oka+=hit
            print(f"{'OK ' if hit else 'NG '} T2a {db:16s} {row['endpoint']}")
        except Exception as e: print(f"NG  T2a {db:16s} ERR:{e}")
    print(f"\nsmoke: T2b OK={okb} / T2a OK={oka}/{na}")

def cmd_verify(a):
    reg=load_registry(a.registry); cov=load_coverage()
    if cov is None: print("先に discover を実行してください"); return
    by={}
    for ln in open(a.__dict__["in"]):
        ln=ln.strip()
        if not ln or ln.startswith("#"): continue
        ds,i=ln.split("\t")[:2]; by.setdefault(ds,set()).add(i)
    cache={}; conf=pend=0
    with open(a.out,"w") as w:
        w.write("dataset\tid\tstatus\toracle\n")
        for ds,ids in by.items():
            ids=sorted(ids)
            rb=t2b_verify(ds,ids,cov,cache,a.batch,a.sleep)
            db=resolve_db(ds,reg)
            need=[i for i in ids if not rb.get(i)]
            ra=t2a_verify(db,need,reg,cache,a.batch,a.sleep,a.object) if db else {}
            for i in ids:
                if rb.get(i):   st,o="confirmed","togoid_label"
                elif ra.get(i): st,o="confirmed","rdfportal_native"
                else:           st,o="pending","-"
                conf+=st=="confirmed"; pend+=st=="pending"
                w.write(f"{ds}\t{i}\t{st}\t{o}\n")
    print(f"confirmed={conf} pending={pend}  (pending は T3送り。absentは判定しない)")

def cmd_diagnose(a):
    reg=load_registry(a.registry)
    targets=[a.db] if a.db else [d for d,r in sorted(reg.items())
             if r.get("queryable") and r.get("example")]
    def q(ep,s):
        try: return sparql(ep,s)["results"]["bindings"]
        except Exception as e: return ("ERR",str(e))
    for db in targets:
        r=reg.get(db)
        if not r: print(f"?? {db}"); continue
        ns=r["subj_uri_ns"]; ep=r["endpoint"]; g=r.get("graph")
        uri=ns+r["example"]; frm=f"FROM <{g}>" if g else ""
        b0=q(ep, build_query(r,[uri]))
        if isinstance(b0,tuple): print(f"NG {db:16s} ENDPOINTエラー: {b0[1]}"); continue
        if b0: print(f"OK {db:16s}"); continue
        bNoG=q(ep, f"SELECT ?p WHERE {{ <{uri}> ?p ?o }} LIMIT 1")
        bObj=q(ep, f"SELECT ?x {frm} WHERE {{ ?x ?p <{uri}> }} LIMIT 1")
        bG  =q(ep, f"SELECT DISTINCT ?g WHERE {{ GRAPH ?g {{ <{uri}> ?p ?o }} }} LIMIT 3")
        if bNoG and not isinstance(bNoG,tuple):
            gs=[x['g']['value'] for x in bG] if (bG and not isinstance(bG,tuple)) else []
            print(f"NG {db:16s} graph不一致 → graph= {gs}")
        elif bObj and not isinstance(bObj,tuple):
            print(f"NG {db:16s} 目的語位置のみ → --object")
        else:
            bs=q(ep, f"SELECT ?s {frm} WHERE {{ ?s ?p ?o }} LIMIT 3")
            ex=[x['s']['value'] for x in bs] if (bs and not isinstance(bs,tuple)) else []
            print(f"NG {db:16s} native URI不一致 → subj_uri_ns要修正 (期待 <{uri}>)")
            if ex: print(f"     graph内の主語例: {ex}")

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    d=sub.add_parser("discover"); d.add_argument("--togoid",default=TOGOID_YAML)
    d.add_argument("--refresh",action="store_true"); d.set_defaults(func=cmd_discover)
    for name,fn in (("smoke",cmd_smoke),("verify",cmd_verify),("diagnose",cmd_diagnose)):
        p=sub.add_parser(name); p.add_argument("--registry",default=REGISTRY)
        p.add_argument("--object",action="store_true")
        if name=="smoke": p.add_argument("--limit",type=int,default=30)
        if name=="diagnose": p.add_argument("--db",default=None)
        if name=="verify":
            p.add_argument("--in",required=True); p.add_argument("--out",default="verified.tsv")
            p.add_argument("--batch",type=int,default=400); p.add_argument("--sleep",type=float,default=0.15)
        p.set_defaults(func=fn)
    a=ap.parse_args(); a.func(a)

if __name__=="__main__": main()
