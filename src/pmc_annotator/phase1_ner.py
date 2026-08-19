"""
Phase 1: NER 全件処理 (GPU並列の単一ワーカー). v2
  - run に --shards-file と --global-range を追加
  - linker-mode のデフォルトを none に変更
"""
from __future__ import annotations
import argparse
import json
import time
import sys
from pathlib import Path
from typing import Optional

from .preprocess import JATSParser, iter_jats_files
from .io_utils import write_shard_parquet, shard_paths, make_shard_id


PLAN_FILE = "shard_plan.json"


def make_plan(input_dir: Path, output_dir: Path, shard_size: int) -> dict:
    xml_files = sorted(iter_jats_files(input_dir))
    plan = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "shard_size": shard_size,
        "total_files": len(xml_files),
        "shards": [],
    }
    for shard_idx, start in enumerate(range(0, len(xml_files), shard_size)):
        chunk = xml_files[start:start + shard_size]
        shard_id = make_shard_id(shard_idx)
        plan["shards"].append({
            "shard_id": shard_id,
            "n_files": len(chunk),
            "files": [str(p) for p in chunk],
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / PLAN_FILE
    with open(plan_path, "w") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    print(f"[plan] {len(xml_files)} files → {len(plan['shards'])} shards "
          f"(size={shard_size}) → {plan_path}")
    return plan


def load_plan(output_dir: Path) -> dict:
    plan_path = output_dir / PLAN_FILE
    with open(plan_path) as f:
        return json.load(f)


def process_shard(shard_entry: dict,
                  output_dir: Path,
                  annotator,
                  parser: JATSParser,
                  with_passages: bool = False) -> dict:
    shard_id = shard_entry["shard_id"]
    paths = shard_paths(output_dir, shard_id, with_passages=with_passages)

    done_marker = paths["annotations"].with_suffix(paths["annotations"].suffix + ".done")
    if done_marker.exists():
        return {"shard_id": shard_id, "status": "skipped", "n_docs": 0, "n_anns": 0,
                "elapsed_s": 0.0}

    t0 = time.time()

    docs = []
    for xml_path_str in shard_entry["files"]:
        try:
            doc = parser.parse(Path(xml_path_str))
            if doc is not None:
                docs.append(doc)
        except Exception as e:
            print(f"[parse skip] {xml_path_str}: {type(e).__name__}: {e}", flush=True)

    t_parse = time.time() - t0

    # NER
    t1 = time.time()
    ok_docs = []
    for doc in docs:
        try:
            annotator(doc)
            ok_docs.append(doc)
        except Exception as e:
            print(f"[ner skip] {doc.id}: {type(e).__name__}: {e}", flush=True)
    docs = ok_docs
    t_ner = time.time() - t1


    t2 = time.time()
    write_shard_parquet(
        docs,
        shard_id=shard_id,
        annotations_path=paths["annotations"],
        documents_path=paths["documents"],
        passages_path=paths.get("passages"),
    )
    t_write = time.time() - t2

    n_anns = sum(len(p.annotations) for d in docs for p in d.passages)
    n_chars = sum(len(p.text) for d in docs for p in d.passages)

    return {
        "shard_id": shard_id,
        "status": "ok",
        "n_docs": len(docs),
        "n_chars": n_chars,
        "n_anns": n_anns,
        "elapsed_s": time.time() - t0,
        "t_parse": t_parse,
        "t_ner": t_ner,
        "t_write": t_write,
    }


def _resolve_target_shards(plan: dict, args) -> list:
    all_shards = plan["shards"]

    if getattr(args, "shards_file", None):
        ids = []
        text = Path(args.shards_file).read_text()
        for tok in text.replace(",", "\n").split("\n"):
            tok = tok.strip()
            if tok:
                ids.append(tok)
        idset = set(ids)
        return [s for s in all_shards if s["shard_id"] in idset]

    if args.shards:
        idset = set(args.shards.split(","))
        return [s for s in all_shards if s["shard_id"] in idset]

    if args.shard_range:
        lo, hi = args.shard_range.split(":")
        return [s for s in all_shards if int(lo) <= int(s["shard_id"]) < int(hi)]

    if getattr(args, "global_range", None):
        lo, hi = args.global_range.split(":")
        return [s for s in all_shards if int(lo) <= int(s["shard_id"]) < int(hi)]

    return all_shards


def cmd_run(args):
    from .annotate_hunflair import HunFlairAnnotator

    output_dir = Path(args.output)
    plan = load_plan(output_dir)

    target_shards = _resolve_target_shards(plan, args)

    print(f"[run] device={args.device}, target shards={len(target_shards)}, "
          f"linker_mode={args.linker_mode}")
    if target_shards:
        print(f"[run] shard range: {target_shards[0]['shard_id']} .. "
              f"{target_shards[-1]['shard_id']}")

    annotator = HunFlairAnnotator(
        device=args.device,
        linker_mode=args.linker_mode,
        short_threshold=args.short_threshold,
        mini_batch_size=args.mini_batch_size,
    )
    annotator.load()
    parser = JATSParser(include_captions=True, include_tables_text=False)

    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    worker_id = args.device.replace(":", "_").replace("/", "_")
    log_path = log_dir / f"worker_{worker_id}.jsonl"

    total = {"n_docs": 0, "n_chars": 0, "n_anns": 0, "elapsed_s": 0.0,
             "ok": 0, "skipped": 0}
    t_start = time.time()

    with open(log_path, "a") as log_f:
        for i, shard_entry in enumerate(target_shards):
            stats = process_shard(shard_entry, output_dir, annotator, parser,
                                  with_passages=args.with_passages)
            log_f.write(json.dumps(stats) + "\n")
            log_f.flush()

            if stats["status"] == "ok":
                total["ok"] += 1
                for k in ("n_docs", "n_chars", "n_anns", "elapsed_s"):
                    total[k] += stats.get(k, 0)
                cps = stats["n_chars"] / max(stats["t_ner"], 0.01)
                print(f"[run][{i+1}/{len(target_shards)}] shard {stats['shard_id']}: "
                      f"{stats['n_docs']} docs, {stats['n_anns']} anns, "
                      f"{stats['elapsed_s']:.1f}s "
                      f"(parse={stats['t_parse']:.1f}, ner={stats['t_ner']:.1f}, "
                      f"write={stats['t_write']:.1f}, ner_speed={cps:.0f} chars/s)",
                      flush=True)
            else:
                total["skipped"] += 1
                print(f"[run][{i+1}/{len(target_shards)}] shard {stats['shard_id']}: skipped",
                      flush=True)

    t_total = time.time() - t_start
    print(f"\n[run] DONE. processed={total['ok']}, skipped={total['skipped']}, "
          f"docs={total['n_docs']}, chars={total['n_chars']:,}, anns={total['n_anns']:,}, "
          f"wall={t_total:.1f}s")
    if total["elapsed_s"] > 0:
        print(f"[run] effective NER throughput: "
              f"{total['n_chars']/total['elapsed_s']:.0f} chars/s")


def cmd_plan(args):
    make_plan(Path(args.input), Path(args.output), args.shard_size)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--input", required=True)
    p_plan.add_argument("--output", required=True)
    p_plan.add_argument("--shard-size", type=int, default=500)

    p_run = sub.add_parser("run")
    p_run.add_argument("--output", required=True)
    p_run.add_argument("--device", default="cuda:0")
    p_run.add_argument("--linker-mode", default="none",
                       choices=["full", "short", "none"])
    p_run.add_argument("--short-threshold", type=int, default=3)
    p_run.add_argument("--mini-batch-size", type=int, default=32)
    p_run.add_argument("--shards", default=None)
    p_run.add_argument("--shards-file", default=None)
    p_run.add_argument("--shard-range", default=None)
    p_run.add_argument("--global-range", default=None)
    p_run.add_argument("--with-passages", action="store_true")

    args = ap.parse_args()
    if args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
