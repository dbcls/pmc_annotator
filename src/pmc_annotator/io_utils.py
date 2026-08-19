"""
shard 単位の I/O (BioC-JSON + Parquet 両対応).
"""
from __future__ import annotations
import gzip
import json
from pathlib import Path
from typing import Iterator

from .schema import Document, Collection


# ========== BioC-JSON shard ==========

def write_shard_bioc(docs: list[Document], shard_path: Path) -> None:
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(shard_path, "wt", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc.to_bioc(), ensure_ascii=False))
            f.write("\n")
    marker = shard_path.with_suffix(shard_path.suffix + ".done")
    marker.touch()


write_shard = write_shard_bioc  # 旧名


def iter_shard(shard_path: Path) -> Iterator[dict]:
    with gzip.open(shard_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


# ========== Parquet shard ==========

def docs_to_parquet_rows(docs: list[Document], shard_id: str):
    annotation_rows = []
    document_rows = []

    for doc in docs:
        n_anns = 0
        total_chars = sum(len(p.text) for p in doc.passages)
        for p_idx, passage in enumerate(doc.passages):
            passage_type = passage.infon.get("section_type", "body")
            for ann in passage.annotations:
                annotation_rows.append({
                    "doc_id": doc.id,
                    "pmid": doc.pmid,
                    "shard_id": shard_id,
                    "passage_idx": p_idx,
                    "passage_type": passage_type,
                    "ann_id": ann.id,
                    "surface": ann.text,
                    "entity_type": ann.entity_type,
                    "offset": ann.offset,
                    "length": ann.length,
                    "score": ann.score,
                    "identifier": ann.identifiers[0] if ann.identifiers else None,
                    "source": ann.source,
                })
                n_anns += 1
        document_rows.append({
            "doc_id": doc.id,
            "pmid": doc.pmid,
            "doi": doc.doi,
            "shard_id": shard_id,
            "journal": doc.infon.get("journal"),
            "year": doc.infon.get("year"),
            "total_chars": total_chars,
            "n_passages": len(doc.passages),
            "n_annotations": n_anns,
        })

    return annotation_rows, document_rows


def write_shard_parquet(docs: list[Document],
                        shard_id: str,
                        annotations_path: Path,
                        documents_path: Path,
                        passages_path=None) -> None:
    import pandas as pd

    ann_rows, doc_rows = docs_to_parquet_rows(docs, shard_id)

    annotations_path.parent.mkdir(parents=True, exist_ok=True)
    documents_path.parent.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(ann_rows, columns=[
        "doc_id", "pmid", "shard_id", "passage_idx", "passage_type",
        "ann_id", "surface", "entity_type", "offset", "length", "score",
        "identifier", "source"
    ]).to_parquet(annotations_path, index=False, compression="zstd")

    pd.DataFrame(doc_rows, columns=[
        "doc_id", "pmid", "doi", "shard_id", "journal", "year",
        "total_chars", "n_passages", "n_annotations"
    ]).to_parquet(documents_path, index=False, compression="zstd")

    if passages_path is not None:
        passages_path.parent.mkdir(parents=True, exist_ok=True)
        passage_rows = []
        for doc in docs:
            for p_idx, passage in enumerate(doc.passages):
                passage_rows.append({
                    "doc_id": doc.id,
                    "shard_id": shard_id,
                    "passage_idx": p_idx,
                    "passage_type": passage.infon.get("section_type", "body"),
                    "offset": passage.offset,
                    "text": passage.text,
                })
        pd.DataFrame(passage_rows, columns=[
            "doc_id", "shard_id", "passage_idx", "passage_type", "offset", "text"
        ]).to_parquet(passages_path, index=False, compression="zstd")

    marker = annotations_path.with_suffix(annotations_path.suffix + ".done")
    marker.touch()


def shard_done(shard_path: Path) -> bool:
    marker = shard_path.with_suffix(shard_path.suffix + ".done")
    return marker.exists()


def make_shard_id(index: int, width: int = 6) -> str:
    return str(index).zfill(width)


def shard_paths(out_dir: Path, shard_id: str,
                with_passages: bool = False) -> dict:
    paths = {
        "annotations": out_dir / "annotations" / f"shard_{shard_id}.parquet",
        "documents":   out_dir / "documents"   / f"shard_{shard_id}.parquet",
    }
    if with_passages:
        paths["passages"] = out_dir / "passages" / f"shard_{shard_id}.parquet"
    return paths
