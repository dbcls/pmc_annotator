"""
End-to-end (Stage A のみ) の動作確認.
HunFlair2 が無くても動く範囲をテスト。
"""
import sys
import tempfile
import shutil
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pmc_annotator.pipeline import stage_preprocess
from pmc_annotator.io_utils import iter_shard


def test_stage_preprocess():
    sample = Path(__file__).parent / "sample_pmc.xml"

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        input_dir = td / "input"
        output_dir = td / "intermediate"
        input_dir.mkdir()

        for i in range(5):
            shutil.copy(sample, input_dir / f"sample_{i}.xml")

        stage_preprocess(input_dir, output_dir, shard_size=2, n_workers=2)

        shards = sorted(output_dir.glob("shard_*.jsonl.gz"))
        markers = sorted(output_dir.glob("shard_*.done"))
        print(f"\nGenerated shards: {[s.name for s in shards]}")
        print(f"Done markers: {[m.name for m in markers]}")

        assert len(shards) == 3
        assert len(markers) == 3

        total = 0
        for shard in shards:
            for d in iter_shard(shard):
                total += 1
                assert d["id"] == "PMC9999999"
                assert len(d["passages"]) > 0
        assert total == 5
        print(f"Total documents in shards: {total}")

        print("\n--- Re-running to verify idempotency ---")
        stage_preprocess(input_dir, output_dir, shard_size=2, n_workers=2)


if __name__ == "__main__":
    test_stage_preprocess()
    print("\n✓ test_stage_preprocess passed")
