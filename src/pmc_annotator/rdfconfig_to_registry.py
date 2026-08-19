#!/usr/bin/env python3
"""rdf-config/config/ から T2実在性検証用 registry を生成。
各DB: endpoint(公開URL置換済)+named graph+native主語URIテンプレ+実例ID+loaded/queryable。
存在確認は untyped(namespace+graph)方針なので type は保持しない。"""
import os, re, json, sys

CFG = sys.argv[1] if len(sys.argv)>1 else "rdf-config-master/config"
GROUPS=['primary','ddbj','pdb','kero','ncbi','ebi','sib','pubchem','togoid','bioportal']
PUBLIC={g:f'https://rdfportal.org/{g}/sparql' for g in GROUPS}
ENDPOINT_OVERRIDE={            # 内部/devプレースホルダ -> 公開エンドポイント
    'sra_experiment': PUBLIC['ddbj'],   # SRA=DDBJ (virtuoso:8890 の実体)
    'togoid':         PUBLIC['togoid'],
    # dbvar(test91), hop(orth sparql-dev) は本番未確定 -> needs_review
}

def read(p):
    try: return open(p, encoding="utf-8", errors="replace").read()
    except FileNotFoundError: return ""

def parse_prefix(txt):
    pm={}
    for ln in txt.splitlines():
        m=re.match(r'\s*([A-Za-z0-9_.\-]+)\s*:\s*<([^>]+)>', ln)
        if m: pm[m.group(1)]=m.group(2)
    return pm

def parse_endpoint(txt):
    loaded = not bool(re.search(r'not\s+yet\s+loaded|TODO', txt, re.I))
    ep=None; graph=None
    for m in re.finditer(r'https?://[^\s#]+sparql[^\s#]*', txt):
        ep=ep or m.group(0)
    mg=re.search(r'graph:\s*(.*)', txt, re.S)
    if mg:
        for g in re.findall(r'https?://[^\s#]+', mg.group(1)):
            if 'sparql' not in g: graph=g; break
    return ep, graph, loaded

def parse_subject(txt, pm):
    """先頭stanzaの主語行から native URI namespace と 実例ID を取る。型は取らない。"""
    for ln in txt.splitlines():
        m=re.match(r'-\s+\S+\s+([A-Za-z0-9_.\-]+):(\S+?):\s*$', ln) or \
          re.match(r'-\s+\S+\s+([A-Za-z0-9_.\-]+):(\S+?):', ln)
        if m:
            return pm.get(m.group(1)), m.group(2)
    return None, None

rows=[]
for d in sorted(os.listdir(CFG)):
    dd=os.path.join(CFG,d)
    if not os.path.isdir(dd): continue
    pm=parse_prefix(read(f"{dd}/prefix.yaml"))
    ep,graph,loaded=parse_endpoint(read(f"{dd}/endpoint.yaml"))
    subj_ns,example=parse_subject(read(f"{dd}/model.yaml"), pm)
    if d in ENDPOINT_OVERRIDE: ep=ENDPOINT_OVERRIDE[d]
    needs_review = bool(ep) and ('test91' in ep or 'sparql-dev' in ep or 'virtuoso:' in ep)
    rows.append(dict(db=d, endpoint=ep, graph=graph, loaded=loaded,
                     subj_uri_ns=subj_ns, example=example,
                     queryable=bool(ep) and bool(subj_ns) and loaded and not needs_review,
                     needs_review=needs_review))

json.dump(rows, open("t2_registry.json","w"), ensure_ascii=False, indent=1)
q=[r for r in rows if r['queryable']]
print(f"DB total={len(rows)}  queryable(loaded&公開&URI導出可)={len(q)}")
print("要確認(内部/dev):", [r['db'] for r in rows if r['needs_review']])
print("subj_uri_ns欠落(loaded&公開だがURI不明):",
      [r['db'] for r in rows if r['endpoint'] and r['loaded'] and not r['subj_uri_ns'] and not r['needs_review']])
for r in rows:
    if r['db'] in ('uniprot','clinvar','chebi','pdb','ensembl','go','chembl','sra_experiment'):
        print(f"{r['db']:14s} {str(r['endpoint']):38s} <{r['subj_uri_ns']}{{id}}> ex={r['example']}")
