#!/usr/bin/env python3
"""
epmc_togoid_diff.py — EuropePMC TextMinedTerms のカバレッジ DB 種を実体から取得し、
TogoID dataset.yaml (118/119 DB) と突き合わせて差分を出す。

出力する 3 バケツ:
  A. EPMC にあり TogoID にない  -> TogoID 側の取りこぼし候補
  B. TogoID にあり EPMC にない  -> 我々の差別化カバレッジ (本命)
  C. 両方にある                 -> 重複領域

使い方:
  # 1) まず実体を見る (ファイル一覧 + 先頭ファイルのスキーマ)
  python3 epmc_togoid_diff.py discover
  python3 epmc_togoid_diff.py sniff

  # 2) EPMC 側 DB 種を抽出 (--from-filenames / --from-column どちらかを discover の結果で選ぶ)
  python3 epmc_togoid_diff.py epmc-dbs --from-filenames        > epmc_dbs.txt
  # または列にDB種が入る形式なら:
  # python3 epmc_togoid_diff.py epmc-dbs --from-column SOURCE   > epmc_dbs.txt

  # 3) 突き合わせ (TogoID yaml はローカル優先、なければ GitHub から取得)
  python3 epmc_togoid_diff.py diff \
      --togoid ./togoid_dataset.yaml \
      --epmc-dbs epmc_dbs.txt \
      --out-dir ./diff_out
"""
import sys, os, re, gzip, io, csv, json, argparse, urllib.request, urllib.parse, html.parser, difflib, ftplib
import yaml

EPMC_HTTPS_INDEX = "https://europepmc.org/ftp/TextMinedTerms/"
EPMC_FTP_INDEX   = "ftp://ftp.ebi.ac.uk/pub/databases/pmc/TextMinedTerms/"
TOGOID_YAML_URL  = "https://raw.githubusercontent.com/dbcls/togoid-config/main/config/dataset.yaml"

# EuropePMC のソース DB 名 -> TogoID 側の正規トークン(identifiers.org prefix 等) の手動対応表。
# discover/sniff で実際の名前を見てから埋める。左辺は正規化後(小文字英数のみ)で照合。
EPMC_TO_TOGOID_TOKEN = {
    "embl": "insdc", "ena": "insdc", "gen": "insdc", "genbank": "insdc", "ddbj": "insdc",
    "uniprot": "uniprot", "swissprot": "uniprot", "trembl": "uniprot",
    "pdb": "pdb", "pdbe": "pdb",
    "refseq": "refseq",
    "refsnp": "dbsnp", "dbsnp": "dbsnp",
    "arrayexpress": "arrayexpress",
    "geo": "geo",
    "ensembl": "ensembl",
    "interpro": "interpro",
    "pfam": "pfam",
    "omim": "mim",
    "bioproject": "bioproject",
    "biosample": "biosample",
    "chebi": "chebi",
    "chembl": "chembl",
    "intact": "intact",
    "reactome": "reactome",
    "doi": "doi",
    "ega": "ega",
    "biostudies": "biostudies",
    "gen": "insdc", "gca": "assemblyinsdc",
    "uniparc": "uniparc", "rnacentral": "rnacentral", "rfam": "rfam",
    "cath": "cath", "treefam": "treefam", "go": "go", "efo": "efo",
    "hgnc": "hgnc", "omim": "mim", "orphadata": "orphanet",
    "alphafold": "alphafold", "emdb": "emdb", "complexportal": "complexportal",
    "metabolights": "metabolights", "reactome": "reactome", "rhea": "rhea",
    "pxd": "pride", "gwas": "gwascatalog", "cellosaurus": "cellosaurus",
}

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

# 定型・非データファイル(EBI FTP 共通の告知/チェックサム等)を除外
BOILERPLATE = re.compile(
    r'^(privacy-notice|readme|license|licence|changelog|changes|manifest|'
    r'md5|md5sum|md5checksums?|sha\d*|sha\d*sums?|checksums?)\b', re.I)

def is_data_file(name):
    base = name.rsplit("/",1)[-1]
    if BOILERPLATE.match(base): return False
    return bool(re.search(r'\.(csv|tsv)(\.gz)?$', base, re.I))

# ---------- HTTP index parsing ----------
class _Links(html.parser.HTMLParser):
    def __init__(self): super().__init__(); self.hrefs=[]
    def handle_starttag(self, tag, attrs):
        if tag=="a":
            for k,v in attrs:
                if k=="href" and v: self.hrefs.append(v)

def http_get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent":"epmc-togoid-diff/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

EPMC_FTP_HOST = "ftp.ebi.ac.uk"
EPMC_FTP_PATH = "/pub/databases/pmc/TextMinedTerms/"

def list_dir_ftp(host=EPMC_FTP_HOST, path=EPMC_FTP_PATH):
    ftp=ftplib.FTP(host, timeout=120); ftp.login()
    names=ftp.nlst(path)
    ftp.quit()
    out=[]
    for n in names:
        base=n.rsplit("/",1)[-1]
        if is_data_file(base):
            out.append((base, f"ftp://{host}{path}{base}"))
    return out

def list_dir(index_url=EPMC_HTTPS_INDEX, prefer_ftp=False):
    """HTTPS autoindex を試し、空/失敗なら FTP(ftp.ebi.ac.uk) に自動フォールバック。"""
    if not prefer_ftp:
        try:
            body = http_get(index_url).decode("utf-8", "replace")
            p=_Links(); p.feed(body)
            out=[]
            for h in p.hrefs:
                if h in ("../","/") or h.startswith("?") or h.startswith("#"): continue
                name = h.rsplit("/",1)[-1] or h
                if is_data_file(name):
                    out.append((name, urllib.parse.urljoin(index_url, h)))
            seen=set(); uniq=[]
            for n,u in out:
                if n in seen: continue
                seen.add(n); uniq.append((n,u))
            if uniq:
                sys.stderr.write(f"# source: HTTPS autoindex ({len(uniq)} files)\n")
                return uniq
            sys.stderr.write("# HTTPS autoindex empty -> falling back to FTP\n")
        except Exception as ex:
            sys.stderr.write(f"# HTTPS autoindex failed ({ex}) -> falling back to FTP\n")
    f=list_dir_ftp()
    sys.stderr.write(f"# source: FTP {EPMC_FTP_HOST}{EPMC_FTP_PATH} ({len(f)} files)\n")
    return f

def open_maybe_gz(url_or_path, nbytes=None):
    if re.match(r'^https?://', url_or_path):
        raw = http_get(url_or_path)
    elif url_or_path.startswith("ftp://"):
        with urllib.request.urlopen(url_or_path, timeout=120) as r: raw=r.read()
    else:
        raw = open(url_or_path,"rb").read()
    if url_or_path.endswith(".gz"):
        raw = gzip.decompress(raw)
    if nbytes: raw = raw[:nbytes]
    return raw.decode("utf-8","replace")

def sniff_delim(sample):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except Exception:
        # フォールバック: 最初の行でタブ優先
        first = sample.splitlines()[0] if sample else ""
        return "\t" if first.count("\t")>=first.count(",") else ","

# ---------- subcommands ----------
def cmd_discover(a):
    files = list_dir(a.index, prefer_ftp=getattr(a,'ftp',False))
    print(f"# index: {a.index}")
    print(f"# {len(files)} data files")
    for n,u in files: print(n)
    print("\n# 判定のヒント:")
    stems = [re.sub(r'\.(csv|tsv)(\.gz)?$','',n,flags=re.I) for n,_ in files]
    print(f"#  ファイル名(拡張子除): {stems[:30]}")
    print("#  -> ファイル名が DB 名っぽければ `epmc-dbs --from-filenames`")
    print("#  -> どれも 'part-000..' 等なら中身の列にDB種。`sniff` で列名を確認し `--from-column`")

def cmd_sniff(a):
    files = list_dir(a.index, prefer_ftp=getattr(a,'ftp',False))
    files = [(n,u) for n,u in files if is_data_file(n)]  # 二重ガード
    if not files: sys.exit("no data files")
    if getattr(a,'file',None):
        pick = [(n,u) for n,u in files if n==a.file or n.startswith(a.file)]
        if not pick: sys.exit(f"file not found: {a.file}")
        name,url = pick[0]
    else:
        name,url = files[0]
    print(f"# sniff: {name}\n# url: {url}")
    txt = open_maybe_gz(url, nbytes=a.bytes)
    delim = sniff_delim(txt[:4096])
    print(f"# delimiter: {delim!r}")
    lines = txt.splitlines()
    print("# --- first 8 lines ---")
    for ln in lines[:8]: print(ln)
    # ヘッダ列名の候補
    header = lines[0].split(delim) if lines else []
    print(f"\n# header columns ({len(header)}): {header}")
    dbcol = [c for c in header if re.search(r'(db|source|database|type|accession.?type|resource)', c, re.I)]
    print(f"# DB種っぽい列: {dbcol or '(なし -> ファイル名がDB種の可能性)'}")

def cmd_epmc_dbs(a):
    files = list_dir(a.index, prefer_ftp=getattr(a,'ftp',False))
    dbs=set()
    if a.from_filenames:
        for n,_ in files:
            stem = re.sub(r'\.(csv|tsv)(\.gz)?$','',n,flags=re.I)
            stem = re.sub(r'[-_]?(part|chunk)?[-_]?\d+$','',stem)  # part番号を除去
            if stem: dbs.add(stem)
    elif a.from_column:
        for n,u in files:
            txt = open_maybe_gz(u, nbytes=a.bytes)
            lines = txt.splitlines()
            if not lines: continue
            delim = sniff_delim(txt[:4096])
            header = lines[0].split(delim)
            try: idx = header.index(a.from_column)
            except ValueError:
                # 大文字小文字無視
                idx = next((i for i,c in enumerate(header) if c.lower()==a.from_column.lower()), None)
            if idx is None:
                print(f"# WARN {n}: 列 {a.from_column} なし (header={header})", file=sys.stderr); continue
            rdr = csv.reader(lines[1:], delimiter=delim)
            for row in rdr:
                if len(row)>idx and row[idx].strip(): dbs.add(row[idx].strip())
    else:
        sys.exit("--from-filenames か --from-column SOURCE を指定")
    for d in sorted(dbs, key=str.lower): print(d)

def load_togoid(path_or_url):
    if os.path.exists(path_or_url):
        data = yaml.safe_load(open(path_or_url, encoding="utf-8"))
    else:
        data = yaml.safe_load(http_get(path_or_url).decode("utf-8","replace"))
    out={}
    for ds_id, v in data.items():
        if not isinstance(v, dict): continue
        tokens=set()
        idorg=None
        for p in v.get("prefix",[]) or []:
            uri = p.get("uri","") if isinstance(p,dict) else ""
            m = re.search(r'identifiers\.org/([^/]+)', uri)
            if m:
                idorg = idorg or m.group(1)
                tokens.add(norm(m.group(1)))
                tokens.add(norm(m.group(1).split(".")[0]))  # insdc.gca -> insdc
            # (汎用ホスト名トークン化は誤マッチ源のため無効化)
        tokens.add(norm(ds_id))
        tokens.add(norm(ds_id.split("_")[0]))
        tokens.add(norm(v.get("label","")))
        out[ds_id] = {
            "label": v.get("label",""),
            "category": v.get("category",""),
            "idorg": idorg,
            "tokens": {t for t in tokens if t},
        }
    return out


# ---- 分類 (申請書テーブル用) ----
# TogoID category -> 粗い区分
CATEGORY_CLASS = {
    "Phenotype":"ontology","Anatomy":"ontology","Classification":"ontology",
    "Function":"ontology","Organism":"ontology",
    "Literature":"literature",
}  # 上記以外は "data" 扱い
CLASS_OVERRIDE = {  # dataset id 単位の上書き
    "mbgd_organism":"data","gea":"data","ec":"ontology","taxonomy":"ontology",
    "clo":"ontology","prosite_prorule":"other",
}
# 日本発 / DBCLS・DDBJ・NBDC 系
JAPAN_IDS = {"jga_dataset","jga_study","nbdc_human_db","togovar","nando",
             "mbgd_gene","mbgd_organism","gea"}
# EPMC独自DBの区分 (既知の28件。既定は data、例外のみ指定)
EPMC_CLASS = {"doi":"other"}

def togo_class(ds_id, category):
    if ds_id in CLASS_OVERRIDE: return CLASS_OVERRIDE[ds_id]
    return CATEGORY_CLASS.get(category, "data")

def align(togo, epmc):
    """(matched_pairs, epmc_only, matched_ds, togoid_only) を返す。"""
    tok2ds={}
    for ds_id, meta in togo.items():
        for t in meta["tokens"]:
            tok2ds.setdefault(t, set()).add(ds_id)
    matched_pairs=[]; epmc_only=[]; matched_ds=set(); matched_epmc=set()
    for db in epmc:
        n = norm(db)
        tok = EPMC_TO_TOGOID_TOKEN.get(n, n)
        cand = tok2ds.get(norm(tok)) or tok2ds.get(n)
        if cand:
            for ds in cand:
                matched_pairs.append((db, ds)); matched_ds.add(ds)
            matched_epmc.add(db)
        else:
            epmc_only.append(db)
    togoid_only = sorted(set(togo)-matched_ds, key=str.lower)
    return matched_pairs, epmc_only, matched_ds, matched_epmc, togoid_only

def cmd_diff(a):
    togo = load_togoid(a.togoid)
    epmc = [l.strip() for l in open(a.epmc_dbs, encoding="utf-8") if l.strip() and not l.startswith("#")]
    matched_pairs, epmc_only, matched_ds, matched_epmc, togoid_only = align(togo, epmc)

    os.makedirs(a.out_dir, exist_ok=True)
    # B: 差別化カバレッジ
    with open(os.path.join(a.out_dir,"B_togoid_only.csv"),"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["togoid_id","label","category","idorg"])
        for ds in togoid_only:
            m=togo[ds]; w.writerow([ds,m["label"],m["category"],m["idorg"] or ""])
    # A: TogoID 取りこぼし候補 + 近い TogoID 提案
    all_labels={ds:togo[ds]["label"] for ds in togo}
    with open(os.path.join(a.out_dir,"A_epmc_only.csv"),"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["epmc_db","nearest_togoid_guess"])
        for db in sorted(epmc_only, key=str.lower):
            guess = difflib.get_close_matches(norm(db),
                       [norm(v["label"]) for v in togo.values()], n=1, cutoff=0.6)
            g=""
            if guess:
                g = next((ds for ds,v in togo.items() if norm(v["label"])==guess[0]), "")
            w.writerow([db, g])
    # C: 重複
    with open(os.path.join(a.out_dir,"C_overlap.csv"),"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["epmc_db","togoid_id","togoid_label"])
        for db,ds in sorted(set(matched_pairs)):
            w.writerow([db,ds,togo[ds]["label"]])

    print(f"TogoID datasets           : {len(togo)}")
    print(f"EPMC DB 種                : {len(epmc)}")
    print(f"[C] 重複 (両方)           : {len(matched_ds)} TogoID / {len(matched_epmc)} EPMC")
    print(f"[A] EPMCのみ(取りこぼし)  : {len(epmc_only)}  -> A_epmc_only.csv")
    print(f"[B] TogoIDのみ(差別化)    : {len(togoid_only)}  -> B_togoid_only.csv")
    print(f"\n[B] 差別化カバレッジ(EPMCが抽出対象にしていないDB) 上位:")
    for ds in togoid_only[:40]:
        m=togo[ds]; print(f"  {ds:22s} {m['category']:12s} {m['label']}")
    if epmc_only:
        print(f"\n[A] EPMCにあるがTogoID未マッチ(要手動確認): {epmc_only}")

def cmd_report(a):
    import collections
    togo = load_togoid(a.togoid)
    epmc = [l.strip() for l in open(a.epmc_dbs, encoding="utf-8") if l.strip() and not l.startswith("#")]
    matched_pairs, epmc_only, matched_ds, matched_epmc, togoid_only = align(togo, epmc)

    CLASS_ORDER = ["data","ontology","literature","other"]
    CLASS_JA = {"data":"データ実体DB","ontology":"オントロジー/用語",
                "literature":"文献","other":"その他"}

    # 各バケツを (class -> [items]) に整理
    def togo_items(ids):
        d=collections.defaultdict(list)
        for ds in sorted(ids, key=str.lower):
            m=togo[ds]; c=togo_class(ds, m["category"])
            d[c].append((ds, m["label"], m["category"], ds in JAPAN_IDS))
        return d
    def epmc_items(dbs):
        d=collections.defaultdict(list)
        for db in sorted(dbs, key=str.lower):
            c=EPMC_CLASS.get(norm(db),"data")
            d[c].append((db,"","",False))
        return d

    B = togo_items(togoid_only)          # TogoID独自
    A = epmc_items(epmc_only)            # EPMC独自
    C_epmc = sorted(matched_epmc, key=str.lower)   # 共通(EPMC名基準)

    os.makedirs(a.out_dir, exist_ok=True)

    # --- 明細 CSV ---
    det=os.path.join(a.out_dir,"report_detail.csv")
    with open(det,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["bucket","side","id","label","togoid_category","class","japan"])
        for c,items in B.items():
            for did,lab,cat,jp in items:
                w.writerow(["TogoID_only","togoid",did,lab,cat,c,"Y" if jp else ""])
        for c,items in A.items():
            for did,lab,cat,jp in items:
                w.writerow(["EPMC_only","epmc",did,lab,cat,c,""])
        for db in C_epmc:
            dss=[ds for d,ds in matched_pairs if d==db]
            w.writerow(["overlap","epmc",db,"|".join(sorted(dss)),"", "",""])

    # --- 相補性テーブル (Markdown) ---
    md=os.path.join(a.out_dir,"report_table.md")
    def cnt(d,c): return len(d.get(c,[]))
    with open(md,"w",encoding="utf-8") as f:
        f.write("### EuropePMC TextMinedTerms vs TogoID: 相補性\n\n")
        f.write(f"- EPMC 抽出DB: **{len(epmc)}**  /  TogoID dataset: **{len(togo)}**\n")
        f.write(f"- 共通: EPMC {len(matched_epmc)} DB ↔ TogoID {len(matched_ds)} dataset\n")
        f.write(f"- EPMC独自: **{len(epmc_only)}**  /  TogoID独自: **{len(togoid_only)}**\n\n")
        f.write("| 区分 | EPMC独自 | 共通(EPMC) | TogoID独自 |\n")
        f.write("|---|---:|---:|---:|\n")
        for c in CLASS_ORDER:
            f.write(f"| {CLASS_JA[c]} | {cnt(A,c)} | {'—'} | {cnt(B,c)} |\n")
        f.write(f"| **計** | **{len(epmc_only)}** | **{len(matched_epmc)}** | **{len(togoid_only)}** |\n\n")

        f.write("#### TogoID独自 (EPMCが抽出していない) — 区分別\n")
        for c in CLASS_ORDER:
            items=B.get(c,[])
            if not items: continue
            names=", ".join(f"{did}" for did,_,_,_ in items)
            f.write(f"- **{CLASS_JA[c]}** ({len(items)}): {names}\n")
        jp=[did for c in B for did,_,_,is_j in B[c] if is_j]
        if jp:
            f.write(f"\n**うち日本発資源**: {', '.join(sorted(jp))}\n")

        f.write("\n#### EPMC独自 (TogoIDが持たない) — 区分別\n")
        for c in CLASS_ORDER:
            items=A.get(c,[])
            if not items: continue
            names=", ".join(did for did,_,_,_ in items)
            f.write(f"- **{CLASS_JA[c]}** ({len(items)}): {names}\n")

    # --- コンソール要約 ---
    print(f"EPMC {len(epmc)} DB / TogoID {len(togo)} dataset")
    print(f"共通: EPMC {len(matched_epmc)} / TogoID {len(matched_ds)}")
    print(f"EPMC独自 {len(epmc_only)} / TogoID独自 {len(togoid_only)}")
    print("\n区分         | EPMC独自 | TogoID独自")
    print("-------------|---------:|----------:")
    for c in CLASS_ORDER:
        print(f"{CLASS_JA[c]:12s} | {cnt(A,c):8d} | {cnt(B,c):10d}")
    print(f"\n-> {md}\n-> {det}")

def main():
    ap=argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub=ap.add_subparsers(dest="cmd", required=True)
    d=sub.add_parser("discover"); d.add_argument("--index",default=EPMC_HTTPS_INDEX); d.add_argument("--ftp",action="store_true"); d.set_defaults(func=cmd_discover)
    s=sub.add_parser("sniff"); s.add_argument("--index",default=EPMC_HTTPS_INDEX); s.add_argument("--bytes",type=int,default=200000); s.add_argument("--ftp",action="store_true"); s.add_argument("--file",default=None); s.set_defaults(func=cmd_sniff)
    e=sub.add_parser("epmc-dbs"); e.add_argument("--index",default=EPMC_HTTPS_INDEX)
    e.add_argument("--from-filenames",action="store_true"); e.add_argument("--from-column",default=None)
    e.add_argument("--bytes",type=int,default=None); e.add_argument("--ftp",action="store_true"); e.set_defaults(func=cmd_epmc_dbs)
    f=sub.add_parser("diff"); f.add_argument("--togoid",default=TOGOID_YAML_URL)
    f.add_argument("--epmc-dbs",required=True); f.add_argument("--out-dir",default="./diff_out"); f.set_defaults(func=cmd_diff)
    r=sub.add_parser("report"); r.add_argument("--togoid",default=TOGOID_YAML_URL)
    r.add_argument("--epmc-dbs",required=True); r.add_argument("--out-dir",default="./report_out"); r.set_defaults(func=cmd_report)
    a=ap.parse_args(); a.func(a)

if __name__=="__main__":
    main()
