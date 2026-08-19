"""
品質検証: 自前パイプライン出力 vs PubTator3 API
同じPMC論文を表層形ベースで突き合わせ、検出力とID精度を評価。
"""
import argparse
import json
import time
import sys
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict


PTC_TYPE_MAP = {
    "Gene": "gene", "Disease": "disease", "Chemical": "chemical",
    "Species": "species", "CellLine": "cell_line", "Cell line": "cell_line",
}
COMPARE_TYPES = {"gene", "disease", "chemical", "species", "cell_line"}
PTC_API = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/pmc_export/biocjson"


def fetch_pubtator(pmcids, retries=3, debug=False):
    result = {}
    batch_size = 10
    for i in range(0, len(pmcids), batch_size):
        batch = pmcids[i:i + batch_size]
        url = f"{PTC_API}?pmcids={','.join(batch)}"
        data = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "pmc-annotator-eval/0.1"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read().decode("utf-8")
                break
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                print(f"  [ptc] attempt {attempt+1} failed for batch {i}: {e}")
                time.sleep(2 * (attempt + 1))
        if data is None:
            print(f"  [ptc] giving up on batch {i}")
            continue

        if debug and i == 0:
            print(f"  [debug] response first 800 chars:\n{data[:800]}")
            print(f"  [debug] response total length: {len(data)}")

        parsed_any = False
        try:
            whole = json.loads(data)
            _extract_ptc_doc(whole, result)
            parsed_any = True
        except json.JSONDecodeError:
            pass
        if not parsed_any:
            for line in data.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _extract_ptc_doc(doc, result)
        time.sleep(0.5)
    return result


def _extract_ptc_doc(doc, result):
    docs_to_process = []
    if "PubTator3" in doc:
        docs_to_process = doc["PubTator3"]
    elif "documents" in doc:
        docs_to_process = doc["documents"]
    elif "passages" in doc:
        docs_to_process = [doc]
    else:
        return

    for d in docs_to_process:
        pmcid = None
        infons = d.get("infons", {})
        for key in ("article-id_pmc", "pmc", "pmcid"):
            if key in infons:
                pmcid = infons[key]
                break
        if pmcid is None:
            pmcid = d.get("id")
        if pmcid and not str(pmcid).startswith("PMC"):
            pmcid = f"PMC{pmcid}"

        anns = set()
        for passage in d.get("passages", []):
            for ann in passage.get("annotations", []):
                a_infons = ann.get("infons", {})
                ptc_type = a_infons.get("type", "")
                etype = PTC_TYPE_MAP.get(ptc_type)
                if etype is None or etype not in COMPARE_TYPES:
                    continue
                surface = ann.get("text", "").strip().lower()
                if not surface:
                    continue
                identifier = a_infons.get("identifier") or a_infons.get("Identifier")
                anns.add((surface, etype, identifier))
        if pmcid:
            result[pmcid] = anns


def load_our_annotations(ann_dir, pmcids):
    import duckdb
    glob = str(ann_dir / "shard_*.parquet")
    pmcid_list = "','".join(pmcids)
    rows = duckdb.query(f"""
        SELECT doc_id, surface, entity_type, identifier
        FROM '{glob}'
        WHERE doc_id IN ('{pmcid_list}')
    """).fetchall()
    result = defaultdict(set)
    for doc_id, surface, etype, identifier in rows:
        if etype not in COMPARE_TYPES:
            continue
        result[doc_id].add((surface.strip().lower(), etype, identifier))
    return result


def normalize_id_for_compare(identifier, etype):
    if not identifier:
        return None
    idstr = str(identifier)
    for prefix in ("NCBIGene:", "Gene:", "NCBITaxon:", "Species:", "MESH:", "mesh:"):
        if idstr.startswith(prefix):
            idstr = idstr[len(prefix):]
            break
    if "," in idstr:
        idstr = idstr.split(",")[0]
    if ";" in idstr:
        idstr = idstr.split(";")[0]
    return idstr


def compare_doc(ptc_anns, our_anns):
    ptc_se = {(s, e) for s, e, _ in ptc_anns}
    our_se = {(s, e) for s, e, _ in our_anns}
    both = ptc_se & our_se
    ptc_only = ptc_se - our_se
    our_only = our_se - ptc_se

    ptc_id = {(s, e): normalize_id_for_compare(i, e) for s, e, i in ptc_anns}
    our_id = {(s, e): normalize_id_for_compare(i, e) for s, e, i in our_anns}

    id_match = id_mismatch = id_both_have = 0
    for se in both:
        pid, oid = ptc_id.get(se), our_id.get(se)
        if pid and oid:
            id_both_have += 1
            if pid == oid:
                id_match += 1
            else:
                id_mismatch += 1

    return {
        "ptc_total": len(ptc_se), "our_total": len(our_se),
        "both": len(both), "ptc_only": len(ptc_only), "our_only": len(our_only),
        "id_both_have": id_both_have, "id_match": id_match, "id_mismatch": id_mismatch,
        "ptc_only_examples": sorted(ptc_only)[:10],
        "our_only_examples": sorted(our_only)[:10],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--our-annotations", type=Path, required=True)
    ap.add_argument("--pmcids", default=None)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--output", type=Path, default=Path("pubtator_comparison.json"))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.pmcids:
        pmcids = [p.strip() for p in args.pmcids.split(",")]
    elif args.sample:
        import duckdb
        glob = str(args.our_annotations / "shard_*.parquet")
        rows = duckdb.query(f"""
            SELECT DISTINCT doc_id FROM '{glob}' USING SAMPLE {args.sample} ROWS
        """).fetchall()
        pmcids = [r[0] for r in rows]
        print(f"[sample] {len(pmcids)} PMCIDs: {pmcids[:5]}...")
    else:
        print("--pmcids か --sample を指定してください")
        sys.exit(1)

    print(f"\n[1] PubTator3 から {len(pmcids)} 論文を取得...")
    ptc = fetch_pubtator(pmcids, debug=args.debug)
    print(f"  取得成功: {len(ptc)} 論文")

    print(f"\n[2] 自前アノテーションをロード...")
    ours = load_our_annotations(args.our_annotations, set(pmcids))
    print(f"  ロード成功: {len(ours)} 論文")

    print(f"\n[3] 論文別比較")
    common = sorted(set(ptc.keys()) & set(ours.keys()))
    print(f"  両方にある論文: {len(common)}")
    if not common:
        print(f"  共通論文なし。PTC keys: {list(ptc.keys())[:5]}, Our keys: {list(ours.keys())[:5]}")
        sys.exit(1)

    per_doc = {}
    agg = defaultdict(int)
    for pmcid in common:
        c = compare_doc(ptc[pmcid], ours[pmcid])
        per_doc[pmcid] = c
        for k in ("ptc_total", "our_total", "both", "ptc_only", "our_only",
                  "id_both_have", "id_match", "id_mismatch"):
            agg[k] += c[k]

    print(f"\n[4] 集計 ({len(common)} 論文)")
    print(f"  PTC総検出:   {agg['ptc_total']:,}")
    print(f"  自前総検出:  {agg['our_total']:,}")
    print(f"  両方が検出:  {agg['both']:,}")
    print(f"  PTCのみ:     {agg['ptc_only']:,}")
    print(f"  自前のみ:    {agg['our_only']:,}")
    if agg['ptc_total']:
        print(f"\n  [PTC基準] recall: {100*agg['both']/agg['ptc_total']:.1f}%")
    if agg['our_total']:
        print(f"  [PTC基準] precision: {100*agg['both']/agg['our_total']:.1f}%")
    if agg['id_both_have']:
        print(f"\n  ID一致率: {100*agg['id_match']/agg['id_both_have']:.1f}% "
              f"({agg['id_match']}/{agg['id_both_have']})")

    print(f"\n[5] エンティティタイプ別")
    type_both = defaultdict(int); type_ptc = defaultdict(int); type_our = defaultdict(int)
    for pmcid in common:
        ptc_se = {(s, e) for s, e, _ in ptc[pmcid]}
        our_se = {(s, e) for s, e, _ in ours[pmcid]}
        for s, e in ptc_se: type_ptc[e] += 1
        for s, e in our_se: type_our[e] += 1
        for s, e in (ptc_se & our_se): type_both[e] += 1
    print(f"  {'type':10s} {'PTC':>8s} {'Ours':>8s} {'both':>8s} {'recall':>8s} {'prec':>8s}")
    for etype in sorted(COMPARE_TYPES):
        p, o, b = type_ptc[etype], type_our[etype], type_both[etype]
        rec = f"{100*b/p:.0f}%" if p else "-"
        pre = f"{100*b/o:.0f}%" if o else "-"
        print(f"  {etype:10s} {p:>8d} {o:>8d} {b:>8d} {rec:>8s} {pre:>8s}")

    out = {"n_docs_compared": len(common), "aggregate": dict(agg), "per_doc": per_doc}
    with open(args.output, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[6] 詳細を {args.output} に出力")

    print(f"\n[7] 取りこぼし/過検出のサンプル")
    for pmcid in common[:3]:
        c = per_doc[pmcid]
        print(f"\n  {pmcid}:")
        print(f"    PTC only (自前が逃した): {c['ptc_only_examples'][:8]}")
        print(f"    Ours only (PTC未検出):   {c['our_only_examples'][:8]}")


if __name__ == "__main__":
    main()
