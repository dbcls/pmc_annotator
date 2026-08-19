#!/usr/bin/env python3
"""oa_file_list.csv から (doc_id=PMCID, year) を抽出して doc_year.tsv を作る。
発行年は 'Article Citation' 列(例 'Breast Cancer Res. 2001 Nov 2; 3(1):55-60')から。
※ 'Last Updated' 列は最終更新日で発行年ではないので使わない。
対象は mentions.tsv に出る doc_id のみ(フル一覧をストリーム処理し絞り込む)。

  python3 make_doc_year.py inspect --oa oa_file_list.csv [--mentions mentions.tsv]
  python3 make_doc_year.py build   --oa oa_file_list.csv --mentions mentions.tsv --out doc_year.tsv
"""
import sys, csv, re, argparse

# 'Journal Abbr. YYYY Mon...' の雑誌名直後に来る4桁西暦(1800-2099)を発行年とする。
YEAR_RE = re.compile(r'\.\s((?:19|20)\d{2})\b')
YEAR_ANY = re.compile(r'\b((?:19|20)\d{2})\b')

def load_target_docids(path):
    s=set()
    if not path: return s
    for ln in open(path, encoding="utf-8"):
        p=ln.rstrip("\n").split("\t")
        if p and p[0]: s.add(p[0])
    return s

def extract_year(citation):
    """(year|None, mode)。mode: 'primary'(雑誌名直後) | 'fallback'(任意4桁) | None"""
    m=YEAR_RE.search(citation)
    if m: return m.group(1), "primary"
    m=YEAR_ANY.search(citation)
    if m: return m.group(1), "fallback"
    return None, None

def iter_rows(oa_path, targets):
    with open(oa_path, newline="", encoding="utf-8") as f:
        r=csv.DictReader(f)
        for row in r:
            pmcid=row.get("Accession ID","").strip()
            if targets and pmcid not in targets: continue
            yield pmcid, row.get("Article Citation","")

def cmd_inspect(a):
    targets=load_target_docids(a.mentions)
    import collections
    yr=collections.Counter(); mode=collections.Counter(); noyear=[]; fallback=[]
    n=0
    for pmcid,cit in iter_rows(a.oa, targets):
        n+=1
        y,md=extract_year(cit)
        mode[md]+=1
        if y: yr[int(y)]+=1
        if md=="fallback": fallback.append((pmcid,cit))
        if md is None: noyear.append((pmcid,cit))
    print(f"対象行(targets={'絞込' if targets else '全件'}): {n:,}")
    print(f"年抽出: primary(雑誌名直後)={mode['primary']:,} / fallback(任意4桁)={mode['fallback']:,} / 取得不可={mode[None]:,}")
    if yr:
        ys=sorted(yr)
        print(f"年レンジ: {ys[0]}–{ys[-1]}  中央値付近: {sorted(yr.elements())[len(list(yr.elements()))//2]}")
        print("年次分布(主要):")
        for y in ys:
            bar="#"*min(60, yr[y]//max(1,(max(yr.values())//60)))
            print(f"  {y}  {yr[y]:>6,d} {bar}")
    if fallback[:5]:
        print("\n[fallback例(雑誌名直後で取れず任意4桁を採用)]")
        for p,c in fallback[:5]: print(f"  {p}: {c}")
    if noyear[:5]:
        print("\n[年取得不可例]")
        for p,c in noyear[:5]: print(f"  {p}: {c}")

def cmd_build(a):
    targets=load_target_docids(a.mentions)
    n=w=0
    with open(a.out,"w",encoding="utf-8") as out:
        for pmcid,cit in iter_rows(a.oa, targets):
            n+=1
            y,_=extract_year(cit)
            out.write(f"{pmcid}\t{y if y else 'NA'}\n"); w+=1
    print(f"書出: {w:,} 行 -> {a.out}  (対象doc_id {len(targets):,})" if targets else f"書出: {w:,} 行 -> {a.out}")

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    for name in ("inspect","build"):
        p=sub.add_parser(name)
        p.add_argument("--oa",required=True)
        p.add_argument("--mentions",default=None)          # inspect:任意 / build:必須(下で検査)
        if name=="build":
            p.add_argument("--out",default="doc_year.tsv")
    a=ap.parse_args()
    if a.cmd=="build" and not a.mentions:
        sys.exit("build には --mentions が必要です")
    {"inspect":cmd_inspect,"build":cmd_build}[a.cmd](a)

if __name__=="__main__": main()
