#!/usr/bin/env python3
"""データ利用指標の素データ生成 (クラス分類つき)。
verified.t3.tsv(実在確定) と mentions.tsv(出現) を (dataset,id)=(db,surface) で結合し、
confirmed な参照だけを対象に、論文/DB/エントリ/クラス単位の集計を出す。

クラス(寄託可能性×credit可能性の軸):
  deposited_research_record : 研究成果レコードの寄託 (= deposited data reuse)
  deposited_entity_registry : 新規同定entityの登録   (= deposited entity reuse/adoption)
  curated_entity_registry   : キュレータが命名・同定するレジストリ
  derived_curated           : 注釈・派生の産物
  out_of_scope              : 概念/文献/計算識別子 (指標対象外)
"""
import argparse, duckdb

REUSE_SEMANTICS = {
    "deposited_research_record": "deposited data reuse",
    "deposited_entity_registry": "deposited entity reuse/adoption",
    "curated_entity_registry":   "curated entity reference",
    "derived_curated":           "derived/curated knowledge reference",
    "out_of_scope":              "(out of scope)",
}
CREDIT_DEPOSITED = ("deposited_research_record", "deposited_entity_registry")

DB_CLASS = {
    # deposited_research_record
    "geo_series":"deposited_research_record","geo_sample":"deposited_research_record",
    "sra_experiment":"deposited_research_record","sra_analysis":"deposited_research_record",
    "assembly_insdc":"deposited_research_record",
    # deposited_entity_registry
    "glytoucan":"deposited_entity_registry",
    # curated_entity_registry
    "hgnc":"curated_entity_registry","chebi":"curated_entity_registry",
    "omim_gene":"curated_entity_registry","mirbase":"curated_entity_registry",
    "mirbase_mature":"curated_entity_registry","mgi_allele":"curated_entity_registry",
    "lipidmaps":"curated_entity_registry",
    # derived_curated
    "refseq_rna":"derived_curated","refseq_protein":"derived_curated","refseq_genomic":"derived_curated",
    "ccds":"derived_curated","uniprot":"derived_curated","uniprot_isoform":"derived_curated",
    "uniparc":"derived_curated","ensembl_gene":"derived_curated","ensembl_transcript":"derived_curated",
    "ensembl_protein":"derived_curated","pfam":"derived_curated","interpro":"derived_curated",
    "prosite":"derived_curated","prosite_prorule":"derived_curated","smart":"derived_curated",
    "cog":"derived_curated","reactome_pathway":"derived_curated","drugbank":"derived_curated",
    "intact":"derived_curated","tair":"derived_curated","flybase_gene":"derived_curated",
    "flybase_transcript":"derived_curated","flybase_protein":"derived_curated",
    "zfin_gene":"derived_curated","rgd":"derived_curated","togovar":"derived_curated",
    # out_of_scope
    "go":"out_of_scope","ec":"out_of_scope","mp":"out_of_scope","doid":"out_of_scope",
    "cl":"out_of_scope","sio":"out_of_scope","efo_disease":"out_of_scope",
    "pubmed":"out_of_scope","pmc":"out_of_scope","affy_probeset":"out_of_scope",
    "inchi_key":"out_of_scope",
    # 未分類3件の追記 (実データで out_of_scope 既定に落ちていたもの)
    "bioproject":"deposited_research_record",   # INSDC BioProject (寄託系)
    "hp_inheritance":"out_of_scope",            # HPO遺伝形式サブセット (概念)
    "uberon":"out_of_scope",                    # 解剖オントロジー (概念)
}
CLASS_ORDER = ["deposited_research_record","deposited_entity_registry",
               "curated_entity_registry","derived_curated","out_of_scope"]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--verified", default="verified.t3.tsv")
    ap.add_argument("--mentions", default="mentions.tsv")
    ap.add_argument("--out-dir", default="metrics")
    ap.add_argument("--min-confidence", choices=["all","high"], default="all")
    ap.add_argument("--doc-year", default=None, help="doc_year.tsv (doc_id<TAB>year) 指定時 usage_by_year.tsv を出す")
    a=ap.parse_args()
    import os; os.makedirs(a.out_dir, exist_ok=True)
    c=duckdb.connect()

    c.execute(f"""CREATE VIEW verified AS SELECT * FROM read_csv('{a.verified}',
        delim='\t', header=true,
        columns={{'dataset':'VARCHAR','id':'VARCHAR','status':'VARCHAR','oracle':'VARCHAR'}});""")
    c.execute(f"""CREATE VIEW mentions AS SELECT * FROM read_csv('{a.mentions}',
        delim='\t', header=false,
        columns={{'doc_id':'VARCHAR','db':'VARCHAR','surface':'VARCHAR','offset':'BIGINT','confidence':'VARCHAR'}});""")

    # DB_CLASS を一時表に
    c.execute("CREATE TABLE db_class(dataset VARCHAR, class VARCHAR)")
    c.executemany("INSERT INTO db_class VALUES (?,?)", list(DB_CLASS.items()))

    if a.doc_year:
        c.execute(f"""CREATE VIEW doc_year AS SELECT * FROM read_csv('{a.doc_year}',
            delim='\t', header=false,
            columns={{'doc_id':'VARCHAR','year':'VARCHAR'}});""")

    conf_filter = "AND m.confidence='high'" if a.min_confidence=="high" else ""
    c.execute(f"""CREATE VIEW base AS
      SELECT m.doc_id, v.dataset, v.id, v.oracle, m.confidence,
             COALESCE(k.class,'out_of_scope') AS class
      FROM mentions m
      JOIN verified v ON m.db=v.dataset AND m.surface=v.id
      LEFT JOIN db_class k ON v.dataset=k.dataset
      WHERE v.status='confirmed' {conf_filter};""")

    od=a.out_dir
    def dump(sql,path): c.execute(f"COPY ({sql}) TO '{od}/{path}' (FORMAT CSV, DELIMITER '\t', HEADER TRUE)")

    dump("""SELECT dataset,id,any_value(class) AS class,any_value(oracle) AS oracle,
                   COUNT(*) AS n_mentions,COUNT(DISTINCT doc_id) AS n_docs
            FROM base GROUP BY dataset,id ORDER BY n_docs DESC,n_mentions DESC""","usage_by_entry.tsv")
    dump("""SELECT dataset,any_value(class) AS class,COUNT(DISTINCT id) AS n_unique_entries,
                   COUNT(*) AS n_mentions,COUNT(DISTINCT doc_id) AS n_docs
            FROM base GROUP BY dataset ORDER BY n_unique_entries DESC""","usage_by_db.tsv")
    dump("""SELECT doc_id,COUNT(DISTINCT id) AS n_entries,COUNT(DISTINCT dataset) AS n_dbs,
                   string_agg(DISTINCT dataset,',') AS dbs
            FROM base GROUP BY doc_id ORDER BY n_entries DESC""","usage_by_doc.tsv")
    dump("""SELECT doc_id,dataset,id,any_value(class) AS class,any_value(oracle) AS oracle,
                   any_value(confidence) AS confidence,COUNT(*) AS n_in_doc
            FROM base GROUP BY doc_id,dataset,id ORDER BY doc_id,dataset,id""","usage_long.tsv")
    dump("""SELECT class,COUNT(DISTINCT dataset) AS n_dbs,COUNT(DISTINCT id) AS n_entries,
                   COUNT(*) AS n_mentions,COUNT(DISTINCT doc_id) AS n_docs
            FROM base GROUP BY class ORDER BY n_entries DESC""","usage_by_class.tsv")

    # ---- サマリ ----
    total=c.execute("SELECT COUNT(DISTINCT doc_id) FROM mentions").fetchone()[0]
    def docs_in(where): return c.execute(f"SELECT COUNT(DISTINCT doc_id) FROM base WHERE {where}").fetchone()[0]
    ref=docs_in("TRUE"); n_ref=c.execute("SELECT COUNT(*) FROM base").fetchone()[0]
    n_entry=c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT dataset,id FROM base)").fetchone()[0]
    print("=== データ利用指標 サマリ (confirmed参照のみ) ===")
    print(f"処理文献(mentions中のユニークdoc): {total:,}")
    print(f"confirmed参照 総数 {n_ref:,} / ユニーク実在エントリ {n_entry:,}\n")
    print(f"文献率(=1つ以上その種の参照を持つ文献の割合):")
    print(f"  全データ参照            : {ref:,}  ({ref/total*100:.2f}%)")
    for cl in CLASS_ORDER:
        d=docs_in(f"class='{cl}'")
        print(f"  {cl:26s}: {d:,}  ({d/total*100:.2f}%)")
    cred=docs_in("class IN ('deposited_research_record','deposited_entity_registry')")
    print(f"\n[credit軸] 寄託系(research_record+entity_registry) 文献率: {cred:,} ({cred/total*100:.2f}%)")
    print("  ※ deposited_research_record は現状 GEO/SRA のみ抽出 (PDB/ENA/PRIDE等の抽出器は未実装)")
    print("  → deposit参照率は過小評価の下限値。reuse種別: research_record='deposited data reuse',")
    print("     entity_registry='deposited entity reuse/adoption' で意味を区別すること。")
    print(f"\nクラス別 (db数/エントリ/参照/文献):")
    for cl in CLASS_ORDER:
        r=c.execute(f"""SELECT COUNT(DISTINCT dataset),COUNT(DISTINCT id),COUNT(*),COUNT(DISTINCT doc_id)
                        FROM base WHERE class='{cl}'""").fetchone()
        print(f"  {cl:26s} {r[0]:>3d}db / {r[1]:>7,d} / {r[2]:>7,d} / {r[3]:>6,d}  [{REUSE_SEMANTICS[cl]}]")
    print(f"\noracle別 confirmed参照:")
    for orc,n in c.execute("SELECT oracle,COUNT(*) FROM base GROUP BY oracle ORDER BY 2 DESC").fetchall():
        print(f"  {orc:18s} {n:>9,d}")
    # 未分類db (DB_CLASS 未登録 → out_of_scope 既定に落ちたもの) を明示
    unclassified=[r[0] for r in c.execute(f"""SELECT DISTINCT dataset FROM base
        WHERE dataset NOT IN (SELECT dataset FROM db_class) ORDER BY dataset""").fetchall()]
    if unclassified:
        print(f"\n[未分類db] DB_CLASS に無く out_of_scope 既定 → 要分類: {unclassified}")
    if a.doc_year:
        # doc単位のクラス在否 × year を結合 (年ごとにクラス別文献率)
        c.execute("""CREATE VIEW doc_class AS
            SELECT DISTINCT doc_id, class FROM base;""")
        c.execute("""CREATE VIEW doc_any AS SELECT DISTINCT doc_id FROM base;""")
        # 母数=その年に mentions を持つ全doc (データ参照有無を問わず)。year=NA は除外。
        dump("""SELECT y.year,
                   COUNT(DISTINCT y.doc_id) AS n_docs_total,
                   COUNT(DISTINCT CASE WHEN a.doc_id IS NOT NULL THEN y.doc_id END) AS n_docs_anydata,
                   COUNT(DISTINCT CASE WHEN dc.class='deposited_research_record' THEN y.doc_id END) AS n_deposit_rec,
                   COUNT(DISTINCT CASE WHEN dc.class='deposited_entity_registry' THEN y.doc_id END) AS n_deposit_ent,
                   COUNT(DISTINCT CASE WHEN dc.class='curated_entity_registry' THEN y.doc_id END) AS n_curated,
                   COUNT(DISTINCT CASE WHEN dc.class='derived_curated' THEN y.doc_id END) AS n_derived
                FROM (SELECT DISTINCT m.doc_id, dy.year FROM mentions m
                      JOIN doc_year dy ON m.doc_id=dy.doc_id WHERE dy.year<>'NA') y
                LEFT JOIN doc_any a ON y.doc_id=a.doc_id
                LEFT JOIN doc_class dc ON y.doc_id=dc.doc_id
                GROUP BY y.year ORDER BY y.year""", "usage_by_year.tsv")

        print("\n=== 年次 × クラス 文献率 (year: n_docs / 寄託系% / 派生% / 全データ%) ===")
        rows=c.execute("""
            SELECT y.year, COUNT(DISTINCT y.doc_id) tot,
               COUNT(DISTINCT CASE WHEN dc.class IN ('deposited_research_record','deposited_entity_registry') THEN y.doc_id END) dep,
               COUNT(DISTINCT CASE WHEN dc.class='derived_curated' THEN y.doc_id END) der,
               COUNT(DISTINCT a.doc_id) anyd
            FROM (SELECT DISTINCT m.doc_id, dy.year FROM mentions m
                  JOIN doc_year dy ON m.doc_id=dy.doc_id WHERE dy.year<>'NA') y
            LEFT JOIN doc_any a ON y.doc_id=a.doc_id
            LEFT JOIN doc_class dc ON y.doc_id=dc.doc_id
            GROUP BY y.year ORDER BY y.year""").fetchall()
        for yr,tot,dep,der,anyd in rows:
            print(f"  {yr}  n={tot:>5,d}  寄託系 {dep/tot*100:5.1f}%  派生 {der/tot*100:5.1f}%  全データ {anyd/tot*100:5.1f}%")
        # 標本内トレンド(寄託系%の傾き)を単純比較
        valid=[(int(yr),dep/tot*100) for yr,tot,dep,der,anyd in rows if tot>=50]
        if len(valid)>=2:
            (y0,p0),(y1,p1)=valid[0],valid[-1]
            print(f"\n  標本内トレンド(寄託系文献率, n>=50の年): {y0} {p0:.1f}% → {y1} {p1:.1f}%  (Δ{p1-p0:+.1f}pt)")
            print("  ※ 標本は 1994–2012・中央値2009 に偏る(shard 0-999=PMC若番)。データ公開一般化は主に2010年代後半")
            print("     以降なので、母集団(全PMC)での寄託系文献率は本値より上振れする下限とみなすこと。")
        print(f"  -> {od}/usage_by_year.tsv")

    print(f"\n-> {od}/usage_by_entry.tsv, usage_by_db.tsv, usage_by_doc.tsv, usage_long.tsv, usage_by_class.tsv")

if __name__=="__main__": main()
