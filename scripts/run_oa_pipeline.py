"""
OA全件投入オーケストレーター: Phase 1 → 1.5 → 2 → 3b → 4 を通しで実行.
各Phaseは冪等・再開可能。--from/--to でPhase限定可能。
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
import json
from pathlib import Path


PHASES = ["1", "regex", "1.5", "2", "3b", "4"]


def run(cmd, log_path=None):
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    if log_path:
        with open(log_path, "a") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    else:
        proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed (rc={proc.returncode}): {cmd}")


def count_parquet_rows(glob_path):
    import duckdb
    try:
        return duckdb.query(f"SELECT COUNT(*) FROM '{glob_path}'").fetchone()[0]
    except Exception:
        return 0


def phase_in_range(phase, from_p, to_p):
    idx = PHASES.index(phase)
    return PHASES.index(from_p) <= idx <= PHASES.index(to_p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=None)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--devices", default="cuda:0")
    ap.add_argument("--shard-size", type=int, default=500)
    ap.add_argument("--mini-batch-size", type=int, default=32)
    ap.add_argument("--chunk-size", type=int, default=5000)
    ap.add_argument("--from", dest="from_p", default="1", choices=PHASES)
    ap.add_argument("--to", dest="to_p", default="4", choices=PHASES)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--skip-phase15", action="store_true")
    ap.add_argument("--global-range", default=None,
                    help="Phase 1 で処理する shard 範囲 (例 0:1000)。"
                         "後続Phaseは生成された annotations を自動追従。")
    ap.add_argument("--regex-workers", type=int, default=32,
                    help="Phase Regex の並列worker数 (CPU)")
    ap.add_argument("--skip-regex", action="store_true",
                    help="Phase Regex (accession抽出) をスキップ")
    args = ap.parse_args()

    wd = args.workdir
    wd.mkdir(parents=True, exist_ok=True)
    log_dir = wd / "pipeline_logs"
    log_dir.mkdir(exist_ok=True)

    d_p1   = wd / "phase1"
    d_p15  = wd / "phase1_5"
    d_p2   = wd / "phase2"
    d_p3b  = wd / "phase3b"
    d_p4   = wd / "phase4"
    d_regex = wd / "phase_regex"

    use_p15 = not args.skip_phase15
    ann_source = d_p15 if use_p15 else d_p1

    PY = sys.executable
    t_all = time.time()

    plan_path = d_p1 / "shard_plan.json"
    if not plan_path.exists():
        if not args.input:
            print("[error] shard_plan.json が無く --input も未指定")
            sys.exit(1)
        print(f"\n=== PLAN ===")
        run([PY, "-m", "pmc_annotator.phase1_ner", "plan",
             "--input", str(args.input), "--output", str(d_p1),
             "--shard-size", str(args.shard_size)],
            log_path=log_dir / "plan.log")
    plan = json.loads(plan_path.read_text())
    n_shards = len(plan["shards"])
    n_files = plan["total_files"]
    print(f"[plan] {n_files:,} files → {n_shards:,} shards")

    if args.plan_only:
        print(f"\n[estimate] (125 shard 実績ベース)")
        print(f"  Phase 1 (NER):  ~{n_shards/57.1:.1f} h (4 GPU, 57 shards/h)")
        print(f"  Phase 3b:       表層形数依存 (89min/697k表層形)")
        return

    devices = args.devices

    # Phase 1
    if phase_in_range("1", args.from_p, args.to_p):
        print(f"\n=== PHASE 1: NER (linker_mode=none) ===")
        t0 = time.time()
        p1_cmd = [PY, "-u", "-m", "pmc_annotator.launch_phase1",
                  "--output", str(d_p1),
                  "--devices", devices,
                  "--linker-mode", "none",
                  "--mini-batch-size", str(args.mini_batch_size)]
        if args.global_range:
            p1_cmd += ["--global-range", args.global_range]
        run(p1_cmd, log_path=log_dir / "phase1.log")
        done = len(list((d_p1 / "annotations").glob("shard_*.parquet.done")))
        if args.global_range:
            lo, hi = args.global_range.split(":")
            expected = min(int(hi), n_shards) - int(lo)
            print(f"[phase1] done shards: {done} (target {args.global_range} ~{expected}), {time.time()-t0:.0f}s")
        else:
            print(f"[phase1] done shards: {done}/{n_shards}, {time.time()-t0:.0f}s")
            if done < n_shards:
                print(f"[warn] 未完了 shard あり ({done}/{n_shards})。再実行で再開可能。")

    # ===== Phase Regex: accession 抽出 (GPU不要・NERと独立) =====
    if (not args.skip_regex) and phase_in_range("regex", args.from_p, args.to_p):
        print(f"\n=== PHASE REGEX: accession 抽出 ===")
        t0 = time.time()
        rx_cmd = [PY, "-u", "-m", "pmc_annotator.phase_regex", "run",
                  "--plan", str(d_p1 / "shard_plan.json"),
                  "--output", str(d_regex),
                  "--workers", str(args.regex_workers)]
        if args.global_range:
            rx_cmd += ["--global-range", args.global_range]
        run(rx_cmd, log_path=log_dir / "phase_regex.log")
        n_acc = count_parquet_rows(
            str(d_regex / "accession_annotations" / "shard_*.parquet"))
        print(f"[regex] accessions: {n_acc:,}, {time.time()-t0:.0f}s")

    # Phase 1.5
    if use_p15 and phase_in_range("1.5", args.from_p, args.to_p):
        print(f"\n=== PHASE 1.5: surface クリーン化 ===")
        t0 = time.time()
        run([PY, "-u", "-m", "pmc_annotator.phase1_5_clean",
             "--input-dir", str(d_p1), "--output-dir", str(d_p15)],
            log_path=log_dir / "phase1_5.log")
        docs_link = d_p15 / "documents"
        if not docs_link.exists():
            docs_link.symlink_to((d_p1 / "documents").resolve())
            print(f"[phase1.5] linked documents/")
        print(f"[phase1.5] {time.time()-t0:.0f}s")

    # Phase 2
    if phase_in_range("2", args.from_p, args.to_p):
        print(f"\n=== PHASE 2: 集計 ===")
        t0 = time.time()
        run([PY, "-u", "-m", "pmc_annotator.phase2_aggregate",
             "--phase1-dir", str(ann_source), "--output-dir", str(d_p2)],
            log_path=log_dir / "phase2.log")
        n_unique = count_parquet_rows(str(d_p2 / "unique_surfaces.parquet"))
        print(f"[phase2] unique surfaces: {n_unique:,}, {time.time()-t0:.0f}s")

    # Phase 3b
    if phase_in_range("3b", args.from_p, args.to_p):
        print(f"\n=== PHASE 3b: ユニーク表層形 linker ===")
        t0 = time.time()
        run([PY, "-u", "-m", "pmc_annotator.launch_phase3b",
             "--input", str(d_p2 / "unique_surfaces.parquet"),
             "--output", str(d_p3b), "--devices", devices,
             "--chunk-size", str(args.chunk_size)],
            log_path=log_dir / "phase3b.log")
        n_dict = count_parquet_rows(str(d_p3b / "identifier_dict.parquet"))
        print(f"[phase3b] dict entries: {n_dict:,}, {time.time()-t0:.0f}s")

    # Phase 4
    if phase_in_range("4", args.from_p, args.to_p):
        print(f"\n=== PHASE 4: ID注入 ===")
        t0 = time.time()
        run([PY, "-u", "-m", "pmc_annotator.phase4_inject",
             "--phase1-dir", str(ann_source),
             "--identifier-dict", str(d_p3b / "identifier_dict.parquet"),
             "--output-dir", str(d_p4), "--per-shard"],
            log_path=log_dir / "phase4.log")
        summary_path = d_p4 / "summary.json"
        if summary_path.exists():
            s = json.loads(summary_path.read_text())
            print(f"[phase4] total={s['n_total']:,}, "
                  f"with_id={s['n_with_identifier']:,} "
                  f"({100*s['n_with_identifier']/max(s['n_total'],1):.1f}%)")
        print(f"[phase4] {time.time()-t0:.0f}s")

    print(f"\n=== PIPELINE DONE in {time.time()-t_all:.0f}s "
          f"({(time.time()-t_all)/3600:.1f}h) ===")
    print(f"最終成果物: {d_p4}/annotations/")


if __name__ == "__main__":
    main()
