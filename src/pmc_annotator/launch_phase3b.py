"""
Phase 3b の GPU 並列ランチャー.

entity_type 4種を別々のGPUに割り当てて並列実行する。
GPU 数が 4 未満なら、足りない分は同じGPUに2種を割り当てる
(linker は GPU メモリ的に小さいので問題ない想定)。
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


# entity_type の処理順 (重い順 = gene/chemical 先)
ENTITY_TYPES = ["gene", "chemical", "disease", "species"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--devices", required=True,
                    help="カンマ区切り (例: cuda:0,cuda:1,cuda:2,cuda:3)")
    ap.add_argument("--chunk-size", type=int, default=5000)
    args = ap.parse_args()

    devices = [d.strip() for d in args.devices.split(",") if d.strip()]
    if not devices:
        print("[launch_phase3b] no devices specified")
        sys.exit(1)

    args.output.mkdir(parents=True, exist_ok=True)

    # entity_type を round-robin に GPU へ
    assignments = []
    for i, etype in enumerate(ENTITY_TYPES):
        dev = devices[i % len(devices)]
        assignments.append((dev, etype))

    print(f"[launch_phase3b] assignments:")
    for dev, etype in assignments:
        print(f"  {dev} ← {etype}")

    log_dir = args.output / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    processes = []
    for dev, etype in assignments:
        cmd = [
            sys.executable, "-u", "-m", "pmc_annotator.phase3b_link",
            "--input", str(args.input),
            "--output", str(args.output),
            "--device", dev,
            "--entity-type", etype,
            "--chunk-size", str(args.chunk_size),
        ]
        log_path = log_dir / f"phase3b_{etype}.log"
        log_f = open(log_path, "a")
        print(f"[launch_phase3b] start {etype} on {dev} → {log_path}")
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        processes.append((etype, dev, proc, log_f))

    # 待ち合わせ
    rc_max = 0
    t_start = time.time()
    try:
        while processes:
            time.sleep(5)
            still = []
            for etype, dev, proc, log_f in processes:
                rc = proc.poll()
                if rc is None:
                    still.append((etype, dev, proc, log_f))
                else:
                    log_f.close()
                    rc_max = max(rc_max, rc)
                    print(f"[launch_phase3b] {etype}@{dev} exited rc={rc} "
                          f"(elapsed {time.time()-t_start:.0f}s)")
            processes = still
            if processes:
                print(f"[launch_phase3b] running: "
                      f"{[(e, d) for e, d, _, _ in processes]}", flush=True)
    except KeyboardInterrupt:
        print(f"\n[launch_phase3b] KeyboardInterrupt - terminating...")
        for _, _, proc, log_f in processes:
            proc.terminate()
            log_f.close()
        for _, _, proc, _ in processes:
            proc.wait(timeout=30)
        sys.exit(130)

    # 全部終わったら merge
    print(f"\n[launch_phase3b] all done in {time.time()-t_start:.0f}s, merging...")
    subprocess.check_call([
        sys.executable, "-m", "pmc_annotator.phase3b_link",
        "--input", str(args.input),
        "--output", str(args.output),
        "--merge",
    ])
    sys.exit(rc_max)


if __name__ == "__main__":
    main()
