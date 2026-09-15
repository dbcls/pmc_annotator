#!/usr/bin/env python3
"""
Build proposed mappings between:
  - Europe PMC accession annotations
  - this project's TogoID extraction database keys
  - Identifiers.org namespaces

This is a proposal generator, not an automatic truth source.
Review the output before copying approved rows into europepmc_db_map.yaml.

Inputs
------
1. --togoid-patterns
   src/pmc_annotator/data/togoid_extract_patterns.yaml

2. --epmc-raw
   JSON Lines output created from Europe PMC Annotations API requests.
   Each line may either be:
     {"response": {...}}
   or a raw Europe PMC JSON response.

3. --idorg-registry
   The Identifiers.org resolver dataset JSON downloaded from:
   https://registry.api.identifiers.org/resolutionApi/getResolverDataset

Outputs
-------
--out                 proposed mappings
--ambiguous-out       rows with multiple possible TogoID targets
--unmapped-out        Europe PMC annotations not confidently mapped
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

import yaml


# ---------------------------------------------------------------------------
# Basic URI and text helpers
# ---------------------------------------------------------------------------

IDORG_HOSTS = {"identifiers.org", "www.identifiers.org"}

# Common provider URL forms that are useful when Europe PMC does not return
# an identifiers.org URI directly. Extend this table as you encounter more.
KNOWN_PROVIDER_PREFIXES = [
    (re.compile(r"(?:purl\.uniprot\.org|rest\.uniprot\.org)/uniprot/", re.I), "uniprot"),
    (re.compile(r"(?:rcsb\.org|pdbe\.org).*/pdb/", re.I), "pdb"),
    (re.compile(r"(?:ebi\.ac\.uk|ensembl\.org).*/ensembl", re.I), "ensembl"),
    (re.compile(r"ncbi\.nlm\.nih\.gov/geo", re.I), "geo"),
    (re.compile(r"ncbi\.nlm\.nih\.gov/(?:nuccore|protein)", re.I), "refseq"),
    (re.compile(r"pfam\.xfam\.org", re.I), "pfam"),
    (re.compile(r"ebi\.ac\.uk/interpro", re.I), "interpro"),
]


def strings_in(value: Any) -> Iterable[str]:
    """Yield all strings in an arbitrary JSON-like object."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)


def normalise_pmcid(value: str) -> Optional[str]:
    """Convert PMC:123 or PMC123 into the pipeline form PMC123."""
    value = value.strip()
    if value.startswith("PMC:"):
        value = value[4:]
    if value.startswith("PMC"):
        value = value[3:]
    return f"PMC{value}" if value.isdigit() else None


def prefix_from_identifiers_org_uri(uri: str) -> Optional[str]:
    """
    Extract a namespace from modern and older Identifiers.org URL forms.

    Supported examples:
      https://identifiers.org/uniprot:P12345
      https://identifiers.org/pdb/1ABC
      http://identifiers.org/uniprot/P12345
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None

    if parsed.netloc.lower() not in IDORG_HOSTS:
        return None

    path = unquote(parsed.path.strip("/"))
    if not path:
        return None

    # Modern compact identifier form: namespace:local_id
    first = path.split("/", 1)[0]
    if ":" in first:
        return first.split(":", 1)[0].lower()

    # Older slash form: namespace/local_id
    return first.lower()


def prefix_from_provider_uri(uri: str) -> Optional[str]:
    """Best-effort mapping for familiar provider URLs."""
    for regex, prefix in KNOWN_PROVIDER_PREFIXES:
        if regex.search(uri):
            return prefix
    return None


def clean_surface(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = " ".join(str(value).split()).strip()
    return value or None


# ---------------------------------------------------------------------------
# TogoID pattern input
# ---------------------------------------------------------------------------

def load_togoid_patterns(path: Path) -> List[Dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows: List[Dict[str, Any]] = []

    for bucket in ("safe", "context_required", "excluded"):
        for db, spec in (data.get(bucket) or {}).items():
            if not isinstance(spec, dict):
                continue

            uri = str(spec.get("uri") or "")
            idorg_prefix = prefix_from_identifiers_org_uri(uri)

            # `pattern` is Python-ready in generated extraction YAML.
            text_pattern = spec.get("pattern")
            compiled = None
            if text_pattern:
                try:
                    compiled = re.compile(str(text_pattern), re.I)
                except re.error:
                    pass

            rows.append(
                {
                    "togoid_db": db,
                    "bucket": bucket,
                    "category": spec.get("category", ""),
                    "uri": uri,
                    "idorg_prefix": idorg_prefix,
                    "pattern": compiled,
                    "orig_regex": str(spec.get("orig_regex") or ""),
                }
            )

    return rows


# ---------------------------------------------------------------------------
# Identifiers.org resolver dataset input
# ---------------------------------------------------------------------------

def walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def load_idorg_prefixes(path: Path) -> Set[str]:
    """
    Resolver-dataset schemas can evolve. This deliberately accepts several
    common field names and only builds a known-prefix set for validation.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    prefixes: Set[str] = set()

    for obj in walk_dicts(data):
        for key in ("prefix", "namespacePrefix", "namespace_prefix"):
            value = obj.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                prefixes.add(value.lower())

    return prefixes


# ---------------------------------------------------------------------------
# Europe PMC annotation input
# ---------------------------------------------------------------------------

def annotation_exact(annotation: Dict[str, Any]) -> Optional[str]:
    """Find the annotated text in common Europe PMC OA JSON/JSON-LD shapes."""
    target = annotation.get("target")

    if isinstance(target, dict):
        selector = target.get("selector")
        if isinstance(selector, dict):
            exact = clean_surface(selector.get("exact"))
            if exact:
                return exact

        # Some responses use a list of selectors.
        if isinstance(selector, list):
            for item in selector:
                if isinstance(item, dict):
                    exact = clean_surface(item.get("exact"))
                    if exact:
                        return exact

    # Defensive fallbacks for alternate response serializations.
    for key in ("exact", "text", "surface", "label", "value"):
        exact = clean_surface(annotation.get(key))
        if exact:
            return exact

    return None


def annotation_article(annotation: Dict[str, Any]) -> Optional[str]:
    """Find the source paper and convert it to the pipeline's PMC123 form."""
    target = annotation.get("target")

    candidates: List[str] = []
    if isinstance(target, dict):
        for key in ("source", "id"):
            value = target.get(key)
            if isinstance(value, str):
                candidates.append(value)

    for key in ("articleId", "article_id", "source", "id"):
        value = annotation.get(key)
        if isinstance(value, str):
            candidates.append(value)

    for value in candidates:
        # Find PMC1234567 embedded in Europe PMC URLs or IDs.
        match = re.search(r"PMC(?::|/)?(\d+)", value, flags=re.I)
        if match:
            return f"PMC{match.group(1)}"

    return None


def annotation_uris(annotation: Dict[str, Any]) -> List[str]:
    """
    Collect candidate entity URIs while avoiding the Europe PMC article URL
    itself. Annotation 'body' is normally the best entity URI.
    """
    uris: List[str] = []

    body = annotation.get("body")
    for value in strings_in(body):
        if value.startswith(("http://", "https://")):
            uris.append(value)

    # Fallback: collect non-article URLs in the complete annotation.
    if not uris:
        for value in strings_in(annotation):
            if not value.startswith(("http://", "https://")):
                continue
            if "europepmc.org/article/" in value.lower():
                continue
            uris.append(value)

    return sorted(set(uris))


def looks_like_annotation(obj: Dict[str, Any]) -> bool:
    """
    Keep objects that resemble named-entity annotations rather than arbitrary
    metadata objects in the API response.
    """
    has_target = "target" in obj
    has_body = "body" in obj
    has_selector = isinstance(obj.get("target"), dict) and "selector" in obj["target"]
    return (has_target and has_body) or has_selector


def annotations_in(value: Any) -> Iterable[Dict[str, Any]]:
    """Recursively find annotation-shaped dictionaries."""
    if isinstance(value, dict):
        if looks_like_annotation(value):
            yield value
        for child in value.values():
            yield from annotations_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from annotations_in(child)


def load_epmc_observations(path: Path) -> List[Dict[str, str]]:
    observations: List[Dict[str, str]] = []

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc

        response = record.get("response", record) if isinstance(record, dict) else record

        for ann in annotations_in(response):
            surface = annotation_exact(ann)
            if not surface:
                continue

            doc_id = annotation_article(ann) or ""
            uris = annotation_uris(ann)

            idorg_prefix = None
            entity_uri = ""
            for uri in uris:
                prefix = prefix_from_identifiers_org_uri(uri)
                if prefix:
                    idorg_prefix = prefix
                    entity_uri = uri
                    break

            if not idorg_prefix:
                for uri in uris:
                    prefix = prefix_from_provider_uri(uri)
                    if prefix:
                        idorg_prefix = prefix
                        entity_uri = uri
                        break

            observations.append(
                {
                    "doc_id": doc_id,
                    "surface": surface,
                    "entity_uri": entity_uri or (uris[0] if uris else ""),
                    "idorg_prefix": idorg_prefix or "",
                }
            )

    return observations


# ---------------------------------------------------------------------------
# Mapping evidence and output
# ---------------------------------------------------------------------------

def candidate_togoid_dbs(
    surface: str,
    idorg_prefix: str,
    togoid_rows: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], int, List[str]]]:
    """
    Return possible TogoID rows with a score and evidence list.

    High-confidence evidence:
      - same Identifiers.org prefix declared in the current TogoID pattern file
      - accession text matches the current text-extraction pattern
    """
    candidates: List[Tuple[Dict[str, Any], int, List[str]]] = []

    for row in togoid_rows:
        if row["bucket"] == "excluded":
            continue

        score = 0
        evidence: List[str] = []

        if idorg_prefix and row["idorg_prefix"] == idorg_prefix:
            score += 60
            evidence.append("same_identifiers_org_prefix")

        pattern = row["pattern"]
        if pattern and pattern.fullmatch(surface):
            score += 30
            evidence.append("matches_togoid_extraction_pattern")

        # A text pattern can be a substring pattern; test this as weaker evidence.
        elif pattern and pattern.search(surface):
            score += 15
            evidence.append("partially_matches_togoid_pattern")

        if score:
            candidates.append((row, score, evidence))

    candidates.sort(key=lambda item: (-item[1], item[0]["togoid_db"]))
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--togoid-patterns", required=True, type=Path)
    parser.add_argument("--epmc-raw", required=True, type=Path)
    parser.add_argument("--idorg-registry", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ambiguous-out", type=Path, default=None)
    parser.add_argument("--unmapped-out", type=Path, default=None)
    args = parser.parse_args()

    ambiguous_out = args.ambiguous_out or args.out.with_name(
        args.out.stem + "_ambiguous.tsv"
    )
    unmapped_out = args.unmapped_out or args.out.with_name(
        args.out.stem + "_unmapped.tsv"
    )

    togoid_rows = load_togoid_patterns(args.togoid_patterns)
    idorg_prefixes = load_idorg_prefixes(args.idorg_registry)
    observations = load_epmc_observations(args.epmc_raw)

    # Group observed Europe PMC evidence by inferred Identifiers.org prefix.
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    no_prefix: List[Dict[str, str]] = []

    for obs in observations:
        prefix = obs["idorg_prefix"].lower()
        if prefix:
            grouped[prefix].append(obs)
        else:
            no_prefix.append(obs)

    fieldnames = [
        "epmc_inferred_prefix",
        "togoid_db",
        "identifiers_org_prefix",
        "status",
        "confidence",
        "n_observations",
        "example_surface",
        "example_entity_uri",
        "evidence",
    ]

    proposed: List[Dict[str, str]] = []
    ambiguous: List[Dict[str, str]] = []
    unmapped: List[Dict[str, str]] = []

    for prefix, items in sorted(grouped.items()):
        example = items[0]
        candidates = candidate_togoid_dbs(
            example["surface"], prefix, togoid_rows
        )

        # Count the best candidate choices across all observed surfaces.
        per_db: Counter[str] = Counter()
        per_db_evidence: Dict[str, Set[str]] = defaultdict(set)

        for item in items:
            matches = candidate_togoid_dbs(
                item["surface"], prefix, togoid_rows
            )
            if matches:
                best_score = matches[0][1]
                for row, score, evidence in matches:
                    if score == best_score:
                        per_db[row["togoid_db"]] += 1
                        per_db_evidence[row["togoid_db"]].update(evidence)

        registry_known = prefix in idorg_prefixes
        base_evidence = ["seen_in_europepmc_annotation"]
        if registry_known:
            base_evidence.append("prefix_present_in_identifiers_org_registry")
        else:
            base_evidence.append("prefix_not_found_in_identifiers_org_registry")

        if not per_db:
            unmapped.append(
                {
                    "epmc_inferred_prefix": prefix,
                    "togoid_db": "",
                    "identifiers_org_prefix": prefix,
                    "status": "unmapped",
                    "confidence": "low",
                    "n_observations": str(len(items)),
                    "example_surface": example["surface"],
                    "example_entity_uri": example["entity_uri"],
                    "evidence": ";".join(base_evidence),
                }
            )
            continue

        ranked = per_db.most_common()
        best_db, best_count = ranked[0]
        tied = [db for db, count in ranked if count == best_count]

        row = {
            "epmc_inferred_prefix": prefix,
            "togoid_db": best_db if len(tied) == 1 else ",".join(sorted(tied)),
            "identifiers_org_prefix": prefix,
            "status": "proposed" if len(tied) == 1 else "ambiguous",
            "confidence": (
                "high"
                if len(tied) == 1 and registry_known and best_count == len(items)
                else "medium"
                if len(tied) == 1
                else "low"
            ),
            "n_observations": str(len(items)),
            "example_surface": example["surface"],
            "example_entity_uri": example["entity_uri"],
            "evidence": ";".join(
                base_evidence + sorted(per_db_evidence[best_db])
            ),
        }

        if row["status"] == "proposed":
            proposed.append(row)
        else:
            ambiguous.append(row)

    # Annotations where a namespace could not be inferred at all.
    for item in no_prefix:
        unmapped.append(
            {
                "epmc_inferred_prefix": "",
                "togoid_db": "",
                "identifiers_org_prefix": "",
                "status": "unmapped",
                "confidence": "low",
                "n_observations": "1",
                "example_surface": item["surface"],
                "example_entity_uri": item["entity_uri"],
                "evidence": "could_not_infer_namespace_from_europepmc_annotation",
            }
        )

    def write_tsv(path: Path, rows: List[Dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

    write_tsv(args.out, proposed)
    write_tsv(ambiguous_out, ambiguous)
    write_tsv(unmapped_out, unmapped)

    print(f"Europe PMC accession observations: {len(observations):,}")
    print(f"Proposed mappings: {len(proposed):,} -> {args.out}")
    print(f"Ambiguous mappings: {len(ambiguous):,} -> {ambiguous_out}")
    print(f"Unmapped observations: {len(unmapped):,} -> {unmapped_out}")
    print("\nReview proposed mappings before adding them to europepmc_db_map.yaml.")


if __name__ == "__main__":
    main()