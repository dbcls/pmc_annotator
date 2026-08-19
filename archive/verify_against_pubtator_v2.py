"""
品質検証 v2: 自前パイプライン出力 vs PubTator3 API (マッチング改善版)
  - 表層形正規化 (小文字/ギリシャ文字/記号/空白)
  - ノイズ除外 (1文字/純数字/純記号)
  - 3段階マッチ (exact / substring)
"""
import argparse
import json
import time
import sys
import re
import unicodedata
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

GREEK_MAP = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "ο": "omicron",
    "π": "pi", "ρ": "rho", "σ": "sigma", "τ": "tau", "υ": "upsilon",
    "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "Α": "alpha", "Β": "beta", "Γ": "gamma", "Δ": "delta",
}


def normalize_surface(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    for g, e in GREEK_MAP.items():
        s = s.replace(g.lower(), e)
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", s)
    s = s.strip(" \t\n-_.,;:()[]{}'\"")
    s = re.sub(r"\s+", " ", s)
    return s


def is_noise(surface_norm):
    if not surface_norm:
        return True
    if len(surface_norm) <= 1:
        return True
    if re.fullmatch(r"[\d\W]+", surface_norm):
        return True
    if re.fullmatch(r"[\d.,]+", surface_norm):
        return True
    if not re.search(r"[a-z0-9]", surface_norm):
        return True
    return False


def fetch_pubtator(pmcids, retries=3, debug=False):
    result = {}
    batch_size = 10
    for i in range(0, len(pmcids), batch_size):
        batch = pmcids[i:i + batch_size]
        url = f"{PTC_API}?pmcids={','.join(batch)}"
        data = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "pmc-annotator-eval/0.2"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read().decode("utf-8")
                break
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                print(f"  [ptc] attempt {attempt+1} failed batch {i}: {e}")
                time.sleep(2 * (attempt + 1))
        if data is None:
            continue
        if debug and i == 0:
            print(f"  [debug] first 600 chars:\n{data[:600]}")
        parsed = False
        try:
            _extract_ptc_doc(json.loads(data), result)
            parsed = True
        except json.JSONDecodeError:
            pass
        if not parsed:
            for line in data.strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        _extract_ptc_doc(json.loads(line), result)
                    except json.JSONDecodeError:
                        pass
        time.sleep(0.5)
    return result


def _extract_ptc_doc(doc, result):
    docs = []
    if "PubTator3" in doc:
        docs = doc["PubTator3"]
    elif "documents" in doc:
        docs = doc["documents"]
    elif "passages" in doc:
        docs = [doc]
    else:
        return
    for d in docs:
        pmcid = None
        infons = d.get("infons", {})
        for key in ("article-id_pmc", "pmc", "pmcid"):
            if key in infons:
                pmcid = infons[key]; break
        if pmcid is None:
            pmcid = d.get("id")
        if pmcid and not str(pmcid).startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        anns = set()
        for passage in d.get("passages", []):
            for ann in passage.get("annotations", []):
                a_inf = ann.get("infons", {})
                etype = PTC_TYPE_MAP.get(a_inf.get("type", ""))
                if etype is None or etype not in COMPARE_TYPES:
                    continue
                surf = normalize_surface(ann.get("text", ""))
                if is_noise(surf):
                    continue
                identifier = a_inf.get("identifier") or a_inf.get("Identifier")
                anns.add((surf, etype, identifier))
        if pmcid:
            result[pmcid] = anns


def load_our_annotations(ann_dir, pmcids):
    import duckdb
    glob = str(ann_dir / "shard_*.parquet")
    pmcid_list = "','".join(pmcids)
    rows = duckdb.query(f"""
        SELECT doc_id, surface, entity_type, identifier
        FROM '{glob}' WHERE doc_id IN ('{pmcid_list}')
    """).fetchall()
    result = defaultdict(set)
    noise_count = 0
    for doc_id, surface, etype, identifier in rows:
        if etype not in COMPARE_TYPES:
            continue
        surf = normalize_surface(surface)
        if is_noise(surf):
            noise_count += 1
            continue
        result[doc_id].add((surf, etype, identifier))
    return result, noise_count


def normalize_id(identifier):
    if not identifier:
        return None
    s = str(identifier)
    for p in ("NCBIGene:", "Gene:", "NCBITaxon:", "Species:", "MESH:", "mesh:",
              "CVCL_", "Cellosaurus:"):
        if s.startswith(p):
            s = s[len(p):]; break
    for sep in (",", ";", "|"):
        if sep in s:
            s = s.split(sep)[0]
    return s.strip()


def match_surfaces(ptc_set, our_set):
    ptc_by_type = defaultdict(set)
    our_by_type = defaultdict(set)
    for s, e in ptc_set:
        ptc_by_type[e].add(s)
    for s, e in our_set:
        our_by_type[e].add(s)

    exact = set()
    substring = set()
    ptc_matched = set()
    our_matched = set()

    for etype in set(ptc_by_type) | set(our_by_type):
        p_surfs = ptc_by_type[etype]
        o_surfs = our_by_type[etype]
        common = p_surfs & o_surfs
        for s in common:
            exact.add((s, etype))
            ptc_matched.add((s, etype))
            our_matched.add((s, etype))
        p_remain = p_surfs - common
        o_remain = o_surfs - common
        for ps in p_remain:
            if (ps, etype) in ptc_matched:
                continue
            for os_ in o_remain:
                if (os_, etype) in our_matched:
                    continue
                short, long = (ps, os_) if len(ps) <= len(os_) else (os_, ps)
                if len(short) >= 3 and short in long:
                    substring.add((ps, os_, etype))
                    ptc_matched.add((ps, etype))
                    our_matched.add((os_, etype))
                    break

    ptc_all = {(s, e) for s, e in ptc_set}
    our_all = {(s, e) for s, e in our_set}
    return {
        "exact": exact, "substring": substring,
        "ptc_matched": ptc_matched, "our_matched": our_matched,
        "ptc_only": ptc_all - ptc_matched,
        "our_only": our_all - our_matched,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--our-annotations", type=Path, required=True)
    ap.add_argument("--pmcids", default=None)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--output", type=Path, default=Path("ptc_cmp_v2.json"))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.pmcids:
        pmcids = [p.strip() for p in args.pmcids.split(",")]
    elif args.sample:
        import duckdb
        glob = str(args.our_annotations / "shard_*.parquet")
        rows = duckdb.query(
            f"SELECT DISTINCT doc_id FROM '{glob}' USING SAMPLE {args.sample} ROWS"
        ).fetchall()
        pmcids = [r[0] for r in rows]
        print(f"[sample] {len(pmcids)} PMCIDs")
    else:
        print("--pmcids か --sample が必要"); sys.exit(1)

    print(f"\n[1] PubTator3 取得 ({len(pmcids)} 論文)...")
    ptc = fetch_pubtator(pmcids, debug=args.debug)
    print(f"  取得: {len(ptc)} 論文")

    print(f"\n[2] 自前ロード...")
    ours, noise_count = load_our_annotations(args.our_annotations, set(pmcids))
    print(f"  ロード: {len(ours)} 論文 (ノイズ除外 {noise_count} 件)")

    common = sorted(set(ptc.keys()) & set(ours.keys()))
    print(f"\n[3] 共通論文: {len(common)}")
    if not common:
        print(f"  PTC keys: {list(ptc.keys())[:5]}")
        print(f"  Our keys: {list(ours.keys())[:5]}")
        sys.exit(1)

    agg = defaultdict(int)
    id_match = id_total = 0
    type_stat = defaultdict(lambda: defaultdict(int))

    for pmcid in common:
        ptc_anns = ptc[pmcid]
        our_anns = ours[pmcid]
        ptc_se = {(s, e) for s, e, _ in ptc_anns}
        our_se = {(s, e) for s, e, _ in our_anns}
        m = match_surfaces(ptc_se, our_se)
        agg["ptc_total"] += len(ptc_se)
        agg["our_total"] += len(our_se)
        agg["exact"] += len(m["exact"])
        agg["substring"] += len(m["substring"])
        agg["ptc_only"] += len(m["ptc_only"])
        agg["our_only"] += len(m["our_only"])
        for s, e in ptc_se:
            type_stat[e]["ptc"] += 1
        for s, e in our_se:
            type_stat[e]["our"] += 1
        for s, e in m["exact"]:
            type_stat[e]["match"] += 1
        for ps, os_, e in m["substring"]:
            type_stat[e]["match"] += 1
        ptc_id = {(s, e): normalize_id(i) for s, e, i in ptc_anns}
        our_id = {(s, e): normalize_id(i) for s, e, i in our_anns}
        for (s, e) in m["exact"]:
            pid, oid = ptc_id.get((s, e)), our_id.get((s, e))
            if pid and oid:
                id_total += 1
                if pid == oid:
                    id_match += 1

    matched = agg["exact"] + agg["substring"]
    print(f"\n[4] 集計 ({len(common)} 論文, ノイズ除外後)")
    print(f"  PTC総検出:        {agg['ptc_total']:,}")
    print(f"  自前総検出:       {agg['our_total']:,}")
    print(f"  完全一致:         {agg['exact']:,}")
    print(f"  部分一致:         {agg['substring']:,}")
    print(f"  マッチ合計:       {matched:,}")
    print(f"  PTCのみ(取漏れ):  {agg['ptc_only']:,}")
    print(f"  自前のみ(過検出): {agg['our_only']:,}")
    if agg["ptc_total"]:
        print(f"\n  recall (exact):        {100*agg['exact']/agg['ptc_total']:.1f}%")
        print(f"  recall (exact+substr): {100*matched/agg['ptc_total']:.1f}%")
    if agg["our_total"]:
        print(f"  precision (exact):        {100*agg['exact']/agg['our_total']:.1f}%")
        print(f"  precision (exact+substr): {100*matched/agg['our_total']:.1f}%")
    if id_total:
        print(f"\n  ID一致率 (exact): {100*id_match/id_total:.1f}% ({id_match}/{id_total})")

    print(f"\n[5] エンティティタイプ別 (exact+substring)")
    print(f"  {'type':10s} {'PTC':>7s} {'Ours':>7s} {'match':>7s} {'recall':>8s} {'prec':>8s}")
    for etype in sorted(COMPARE_TYPES):
        p = type_stat[etype]["ptc"]; o = type_stat[etype]["our"]; mt = type_stat[etype]["match"]
        rec = f"{100*mt/p:.0f}%" if p else "-"
        pre = f"{100*mt/o:.0f}%" if o else "-"
        print(f"  {etype:10s} {p:>7d} {o:>7d} {mt:>7d} {rec:>8s} {pre:>8s}")

    print(f"\n[6] 真の差分サンプル (3論文)")
    for pmcid in common[:3]:
        ptc_se = {(s, e) for s, e, _ in ptc[pmcid]}
        our_se = {(s, e) for s, e, _ in ours[pmcid]}
        m = match_surfaces(ptc_se, our_se)
        print(f"\n  {pmcid}:")
        print(f"    PTC only: {sorted(m['ptc_only'])[:8]}")
        print(f"    Our only: {sorted(m['our_only'])[:8]}")

    out = {
        "n_docs": len(common), "aggregate": dict(agg), "matched": matched,
        "id_match": id_match, "id_total": id_total,
        "type_stat": {k: dict(v) for k, v in type_stat.items()},
    }
    with open(args.output, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[7] {args.output} に出力")


if __name__ == "__main__":
    main()
