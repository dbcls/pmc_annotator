"""
Phase 1 の GPU 並列ランチャー (v2).
  - 担当 shard をファイル経由で渡す (コマンドライン長制限回避)
  - --global-range で処理対象を絞れる
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from .phase1_ner import load_plan


def assign_shards(shard_ids: list, n_workers: int):
    buckets = [[] for _ in range(n_workers)]
    for i, sid in enumerate(shard_ids):
        buckets[i % n_workers].append(sid)
    return buckets


def select_shard_ids(plan: dict, global_range):
    all_ids = [s["shard_id"] for s in plan["shards"]]
    if global_range:
        lo, hi = global_range.split(":")
        return [sid for sid in all_ids if int(lo) <= int(sid) < int(hi)]
    return all_ids


def launch_subprocesses(output_dir: Path, devices: list,
                        linker_mode: str, short_threshold: int,
                        mini_batch_size: int, with_passages: bool,
                        global_range) -> int:
    plan = load_plan(output_dir)
    shard_ids = select_shard_ids(plan, global_range)
    assignments = assign_shards(shard_ids, len(devices))

    print(f"[launch] {len(shard_ids)} shards across {len(devices)} workers")
    if global_range:
        print(f"[launch] global_range={global_range}")
    for dev, assigned in zip(devices, assignments):
        if assigned:
            print(f"  {dev}: {len(assigned)} shards ({assigned[0]}..{assigned[-1]})")

    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    processes = []
    for dev, assigned in zip(devices, assignments):
        if not assigned:
            continue
        worker_id = dev.replace(":", "_").replace("/", "_")
        shards_file = log_dir / f"shards_{worker_id}.txt"
        shards_file.write_text("\n".join(assigned))

        cmd = [
            sys.executable, "-u", "-m", "pmc_annotator.phase1_ner", "run",
            "--output", str(output_dir),
            "--device", dev,
            "--linker-mode", linker_mode,
            "--short-threshold", str(short_threshold),
            "--mini-batch-size", str(mini_batch_size),
            "--shards-file", str(shards_file),
        ]
        if with_passages:
            cmd.append("--with-passages")

        log_path = log_dir / f"stdout_{worker_id}.log"
        log_f = open(log_path, "a")
        print(f"[launch] start {dev}: {len(assigned)} shards → {log_path}")
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        processes.append((dev, proc, log_f))

    rc_max = 0
    t_start = time.time()
    n_total = len(shard_ids)
    try:
        while processes:
            time.sleep(10)
            still = []
            for dev, proc, log_f in processes:
                rc = proc.poll()
                if rc is None:
                    still.append((dev, proc, log_f))
                else:
                    log_f.close()
                    rc_max = max(rc_max, rc)
                    print(f"[launch] {dev} exited rc={rc} (elapsed {time.time()-t_start:.0f}s)")
            processes = still
            if processes:
                done = sum(1 for _ in (output_dir / "annotations").glob("shard_*.parquet.done"))
                el = time.time() - t_start
                rate = done / max(el, 1) * 3600
                print(f"[launch] {[p[0] for p in processes]} | "
                      f"done {done}/{n_total} | {el:.0f}s | ~{rate:.1f} shards/h", flush=True)
    except KeyboardInterrupt:
        print("\n[launch] KeyboardInterrupt - terminating...")
        for dev, proc, log_f in processes:
            proc.terminate()
            log_f.close()
        for dev, proc, _ in processes:
            proc.wait(timeout=30)
        return 130

    print(f"\n[launch] all workers done in {time.time()-t_start:.0f}s")
    return rc_max


def emit_slurm_script(output_dir, devices, linker_mode, short_threshold,
                      mini_batch_size, with_passages, out_path, global_range,
                      slurm_partition="gpu", slurm_time="7-00:00:00"):
    plan = load_plan(output_dir)
    shard_ids = select_shard_ids(plan, global_range)
    n_workers = len(devices)
    assignments = assign_shards(shard_ids, n_workers)

    work_dir = output_dir / "logs"
    work_dir.mkdir(parents=True, exist_ok=True)
    for i, assigned in enumerate(assignments):
        (work_dir / f"slurm_task_{i}.txt").write_text("\n".join(assigned))

    wp = "--with-passages" if with_passages else ""
    script = f"""#!/bin/bash
#SBATCH --job-name=pmc_phase1
#SBATCH --array=0-{n_workers-1}
#SBATCH --partition={slurm_partition}
#SBATCH --gres=gpu:1
#SBATCH --time={slurm_time}
#SBATCH --output={work_dir}/slurm_%A_%a.out
#SBATCH --error={work_dir}/slurm_%A_%a.err

set -euo pipefail
# conda activate hunflair

OUTPUT_DIR={output_dir}
TASK_ID=$SLURM_ARRAY_TASK_ID
SHARDS_FILE=${{OUTPUT_DIR}}/logs/slurm_task_${{TASK_ID}}.txt

python -u -m pmc_annotator.phase1_ner run \\
    --output ${{OUTPUT_DIR}} \\
    --device cuda:0 \\
    --linker-mode {linker_mode} \\
    --short-threshold {short_threshold} \\
    --mini-batch-size {mini_batch_size} \\
    {wp} \\
    --shards-file ${{SHARDS_FILE}}
"""
    out_path.write_text(script)
    out_path.chmod(0o755)
    print(f"[launch] SLURM script: {out_path} ({n_workers} workers, {len(shard_ids)} shards)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--devices", required=True)
    ap.add_argument("--linker-mode", default="none", choices=["full", "short", "none"])
    ap.add_argument("--short-threshold", type=int, default=3)
    ap.add_argument("--mini-batch-size", type=int, default=32)
    ap.add_argument("--with-passages", action="store_true")
    ap.add_argument("--global-range", default=None)
    ap.add_argument("--emit-slurm", type=Path, default=None)
    ap.add_argument("--slurm-partition", default="gpu")
    ap.add_argument("--slurm-time", default="7-00:00:00")
    args = ap.parse_args()

    devices = [d.strip() for d in args.devices.split(",") if d.strip()]

    if args.emit_slurm:
        emit_slurm_script(
            args.output, devices, args.linker_mode, args.short_threshold,
            args.mini_batch_size, args.with_passages, args.emit_slurm,
            args.global_range,
            slurm_partition=args.slurm_partition, slurm_time=args.slurm_time)
    else:
        rc = launch_subprocesses(
            args.output, devices, args.linker_mode, args.short_threshold,
            args.mini_batch_size, args.with_passages, args.global_range)
        sys.exit(rc)


if __name__ == "__main__":
    main()
