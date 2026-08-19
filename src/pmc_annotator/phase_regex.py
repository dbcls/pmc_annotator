"""
Phase Regex (独立レイヤー): XML を再パースして accession を正規表現抽出.
NER パイプライン (Phase 1-4) とは独立。同じ shard_plan・preprocess を使い、
passage.offset 基準の同一座標系で accession アノテーションを出力。GPU不要。
"""
from __future__ import annotations
import argparse
import json
import time
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

from .preprocess import JATSParser
from .regex_annotator import annotate_text


def process_shard_regex(shard_entry: dict, output_dir_str: str) -> dict:
    import pandas as pd
    from pathlib import Path
    from pmc_annotator.preprocess import JATSParser
    from pmc_annotator.regex_annotator import annotate_text

    output_dir = Path(output_dir_str)
    shard_id = shard_entry["shard_id"]
    out_path = output_dir / "accession_annotations" / f"shard_{shard_id}.parquet"
    done_marker = out_path.with_suffix(out_path.suffix + ".done")
    if done_marker.exists():
        return {"shard_id": shard_id, "status": "skipped", "n_acc": 0}

    parser = JATSParser(include_captions=True, include_tables_text=False)
    rows = []
    n_docs = 0
    for xml_path_str in shard_entry["files"]:
        try:
            doc = parser.parse(Path(xml_path_str))
        except Exception:
            continue
        if doc is None:
            continue
        n_docs += 1
        for p_idx, passage in enumerate(doc.passages):
            if not passage.text:
                continue
            hits = annotate_text(passage.text)
            for i, h in enumerate(hits):
                rows.append({
                    "doc_id": doc.id,
                    "pmid": doc.pmid,
                    "shard_id": shard_id,
                    "passage_idx": p_idx,
                    "passage_type": passage.infon.get("section_type", "body"),
                    "ann_id": f"A{i}",
                    "surface": h.surface,
                    "entity_type": "accession",
                    "db": h.db,
                    "offset": passage.offset + h.offset,
                    "length": h.length,
                    "identifier": h.identifier,
                    "confidence": h.confidence,
                    "source": "regex",
                })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=[
        "doc_id", "pmid", "shard_id", "passage_idx", "passage_type",
        "ann_id", "surface", "entity_type", "db", "offset", "length",
        "identifier", "confidence", "source"
    ]).to_parquet(out_path, index=False, compression="zstd")
    done_marker.touch()
    return {"shard_id": shard_id, "status": "ok", "n_docs": n_docs, "n_acc": len(rows)}


def cmd_run(args):
    plan = json.loads(Path(args.plan).read_text())
    shards = plan["shards"]
    if args.global_range:
        lo, hi = args.global_range.split(":")
        shards = [s for s in shards if int(lo) <= int(s["shard_id"]) < int(hi)]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[regex] {len(shards)} shards, {args.workers} workers")

    t0 = time.time()
    total_acc = 0
    total_docs = 0
    done = 0
    out_str = str(output_dir)

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_shard_regex, s, out_str): s["shard_id"]
                   for s in shards}
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r["status"] == "ok":
                total_acc += r["n_acc"]
                total_docs += r.get("n_docs", 0)
            if done % 50 == 0 or done == len(shards):
                el = time.time() - t0
                print(f"[regex] {done}/{len(shards)} shards, "
                      f"{total_acc:,} accessions, {el:.0f}s "
                      f"({done/el*3600:.0f} shards/h)", flush=True)

    print(f"\n[regex] DONE in {time.time()-t0:.0f}s")
    print(f"  docs: {total_docs:,}, accessions: {total_acc:,}")

    try:
        import duckdb
        glob = str(output_dir / "accession_annotations" / "shard_*.parquet")
        print(f"\n[regex] db別内訳:")
        for db, cnt, conf in duckdb.query(f"""
            SELECT db, COUNT(*),
                   COUNT(*) FILTER (WHERE confidence='high') AS high
            FROM '{glob}' GROUP BY db ORDER BY COUNT(*) DESC
        """).fetchall():
            print(f"    {db:12s} {cnt:>10,d} (high: {conf:,})")
    except Exception as e:
        print(f"  [warn] 集計スキップ: {e}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--plan", required=True, help="Phase 1 の shard_plan.json")
    p.add_argument("--output", required=True)
    p.add_argument("--global-range", default=None)
    p.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    if args.cmd == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
