#!/usr/bin/env python3
"""Adapter A: phase_regex_togoid の parquet 出力 -> verify_t2 入力を生成。
  candidates.tsv : ユニーク "dataset<TAB>id"  (= DISTINCT db, surface)   ※ヘッダ無し
  mentions.tsv   : 全件 "doc_id<TAB>db<TAB>surface<TAB>offset<TAB>confidence" ※ヘッダ無し
さらに db 名の齟齬チェック(check-db): 抽出db が verify_t2 側(T2b label graph / T2a registry・ROUTER)で
解決できるかを問い合わせ、結果とASKログを保存する。

  python3 build_candidates.py inspect  --input ./data/oa/phase_regex_togoid
  python3 build_candidates.py check-db --input ./data/oa/phase_regex_togoid \
          --registry t2_registry.json [--togoid-router verify_t2.py]
  python3 build_candidates.py build    --input ./data/oa/phase_regex_togoid \
          --out-candidates candidates.tsv --out-mentions mentions.tsv [--safe-only]
"""
import sys, os, re, json, glob, time, argparse, urllib.request, urllib.parse
import duckdb

BACKEND_PRIMARY = "https://rdfportal.org/backend/primary/sparql"
LABEL_GRAPH = "http://rdfportal.org/dataset/togoid/label/{ds}"
SUBDIR = "accession_annotations_togoid"

def parquet_glob(input_dir):
    g = os.path.join(input_dir, SUBDIR, "shard_*.parquet")
    if not glob.glob(g):                      # 直下にparquetがある場合も許容
        g2 = os.path.join(input_dir, "shard_*.parquet")
        if glob.glob(g2): return g2
    return g

def con(): return duckdb.connect()

def cmd_inspect(a):
    g=parquet_glob(a.input); c=con()
    n=c.execute(f"SELECT COUNT(*) FROM '{g}'").fetchone()[0]
    ndoc=c.execute(f"SELECT COUNT(DISTINCT doc_id) FROM '{g}'").fetchone()[0]
    print(f"parquet glob: {g}")
    print(f"rows(mentions)={n:,}  docs={ndoc:,}")
    print("\ndb別: mention数 / ユニークID数 / 文献数 (上位40)")
    for db,m,u,d in c.execute(f"""
        SELECT db, COUNT(*), COUNT(DISTINCT surface), COUNT(DISTINCT doc_id)
        FROM '{g}' GROUP BY db ORDER BY COUNT(*) DESC LIMIT 40""").fetchall():
        print(f"  {db:22s} {m:>9,d} / {u:>8,d} / {d:>7,d}")
    tot_u=c.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT db,surface FROM '{g}')").fetchone()[0]
    print(f"\nユニーク (db,surface) 総数 = {tot_u:,}  (= candidates.tsv 行数)")

def ask_labelgraph(ds, timeout=30):
    q=f"ASK {{ GRAPH <{LABEL_GRAPH.format(ds=ds)}> {{ ?s ?p ?o }} }}"
    data=urllib.parse.urlencode({"query":q}).encode()
    req=urllib.request.Request(BACKEND_PRIMARY, data=data, headers={
        "Accept":"application/sparql-results+json",
        "Content-Type":"application/x-www-form-urlencoded",
        "User-Agent":"build-candidates/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw=r.read().decode("utf-8","replace")
    return json.loads(raw).get("boolean", False), q, raw

def load_router_and_registry(registry_path, router_src):
    reg=set()
    if os.path.exists(registry_path):
        reg={x["db"] for x in json.load(open(registry_path))}
    router={}
    # verify_t2.py から ROUTER dict を安全に抽出 (import せず literal 評価)
    try:
        src=open(router_src, encoding="utf-8").read()
        m=re.search(r'ROUTER\s*=\s*\{(.*?)\}', src, re.S)
        if m:
            for k,v in re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1)):
                router[k]=v
    except FileNotFoundError:
        pass
    return reg, router

def cmd_check_db(a):
    g=parquet_glob(a.input); c=con()
    dbs=[r[0] for r in c.execute(f"""
        SELECT db, COUNT(*) FROM '{g}' GROUP BY db ORDER BY COUNT(*) DESC""").fetchall()]
    counts=dict(c.execute(f"SELECT db, COUNT(*) FROM '{g}' GROUP BY db").fetchall())
    reg, router = load_router_and_registry(a.registry, a.togoid_router)

    rows=[]; logf=open(a.out_log,"w",encoding="utf-8")
    logf.write(f"# db_check log  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    logf.write(f"# endpoint={BACKEND_PRIMARY}\n# registry={a.registry} router={a.togoid_router}\n\n")
    for db in dbs:
        # T2b: label graph 有無 (ASK)
        try:
            t2b, q, raw = ask_labelgraph(db)
            logf.write(f"[{db}] ASK -> {t2b}\n  Q: {q}\n  R: {raw.strip()[:200]}\n")
        except Exception as e:
            t2b=None; logf.write(f"[{db}] ASK ERROR: {e}\n")
        # T2a: registry 直接 or ROUTER 解決
        resolved = db if db in reg else router.get(db)
        t2a = bool(resolved) and (resolved in reg)
        logf.write(f"  T2a: registry_has={db in reg} router={router.get(db)} resolved={resolved} t2a_ok={t2a}\n\n")
        if t2b:      verdict="T2b"
        elif t2a:    verdict="T2a"
        else:        verdict="MISS(齟齬/T3)"
        rows.append((db, counts.get(db,0), t2b, t2a, resolved or "", verdict))
        time.sleep(a.sleep)
    logf.close()

    with open(a.out_tsv,"w",encoding="utf-8") as w:
        w.write("db\tmentions\tt2b_labelgraph\tt2a_resolved\tt2a_target\tverdict\n")
        for db,m,t2b,t2a,res,v in rows:
            w.write(f"{db}\t{m}\t{t2b}\t{t2a}\t{res}\t{v}\n")

    nb=sum(1 for r in rows if r[5]=="T2b"); na=sum(1 for r in rows if r[5]=="T2a")
    nm=sum(1 for r in rows if r[5].startswith("MISS"))
    print(f"db種={len(rows)}  T2b={nb}  T2a={na}  MISS(齟齬/T3)={nm}")
    print(f"-> {a.out_tsv} / {a.out_log}")
    if nm:
        print("MISS(要確認):", ", ".join(r[0] for r in rows if r[5].startswith("MISS")))

def cmd_build(a):
    g=parquet_glob(a.input); c=con()
    where = "WHERE confidence='high'" if a.safe_only else ""
    # candidates.tsv : DISTINCT db, surface (ヘッダ無し)
    c.execute(f"""COPY (SELECT DISTINCT db, surface FROM '{g}' {where}
                  ORDER BY db, surface)
                  TO '{a.out_candidates}' (FORMAT CSV, DELIMITER '\t', HEADER FALSE)""")
    # mentions.tsv : 全件 (ヘッダ無し)
    c.execute(f"""COPY (SELECT doc_id, db, surface, "offset", confidence FROM '{g}' {where})
                  TO '{a.out_mentions}' (FORMAT CSV, DELIMITER '\t', HEADER FALSE)""")
    nc=sum(1 for _ in open(a.out_candidates, encoding="utf-8"))
    nm=sum(1 for _ in open(a.out_mentions, encoding="utf-8"))
    print(f"candidates.tsv: {nc:,} 行 (ユニーク dataset,id)  -> {a.out_candidates}")
    print(f"mentions.tsv  : {nm:,} 行 (全件)                 -> {a.out_mentions}")
    if a.safe_only: print("(--safe-only: confidence='high' のみ)")

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd", required=True)
    for name in ("inspect","check-db","build"):
        p=sub.add_parser(name); p.add_argument("--input", required=True)
        if name=="check-db":
            p.add_argument("--registry", default="t2_registry.json")
            p.add_argument("--togoid-router", default="verify_t2.py")
            p.add_argument("--out-tsv", default="db_check.tsv")
            p.add_argument("--out-log", default="db_check.log")
            p.add_argument("--sleep", type=float, default=0.05)
        if name=="build":
            p.add_argument("--out-candidates", default="candidates.tsv")
            p.add_argument("--out-mentions", default="mentions.tsv")
            p.add_argument("--safe-only", action="store_true")
    a=ap.parse_args()
    {"inspect":cmd_inspect,"check-db":cmd_check_db,"build":cmd_build}[a.cmd](a)

if __name__=="__main__": main()
