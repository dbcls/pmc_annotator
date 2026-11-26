# Europe PMC extraction layer

This optional layer imports precomputed Europe PMC accession annotations. It can
replace the regex input to candidate generation or merge with it. It does not
replace T2/T3 verification. No additional dependencies beyond the base install.

## Run on the same papers

Activate the project virtual environment. Set these paths in your terminal:

```bash
PLAN=/Users/vsubramoniam/Code/Identifiers-Hackathon/output/output_1/shard_plan.json
RUN=/Users/vsubramoniam/Code/Identifiers-Hackathon/output/epmc_sample
REGEX=/Users/vsubramoniam/Code/Identifiers-Hackathon/output/regex_sample

python -m pmc_annotator.epmc_annotations fetch \
  --plan "$PLAN" --output "$RUN/raw" --include-med

python -m pmc_annotator.epmc_annotations normalize \
  --plan "$PLAN" --raw "$RUN/raw" --output "$RUN/annotations"

python -m pmc_annotator.compare_extractions compare \
  --togoid "$REGEX" --epmc "$RUN/annotations" --output "$RUN/comparison"
```

The fetcher derives PMCID and PMID from each planned XML, requests PMC:<digits>,
and optionally MED:<PMID> (abstract annotations) with --include-med. These are
separate annotation scopes; an empty response for one does not prove absence in
the other. No hasTMAccessionNumbers filter is used. Successful empty results are
cached; network failures are retried and reported as failures. Use --refresh to
fetch cached requests again. Requests use JSON-LD and run one article at a time.
Reference: https://europepmc.org/annotationsapi and the example identifier format
at https://github.com/EuropePMC/epmc-tools/blob/main/docs/cli_usage.rst.

The raw directory contains wrapper JSON files, one per request, including the
local PMCID and retrieval date. Earlier hand-created JSONL caches are not inputs
to this command. Unexpected API response structures fail explicitly.

## Choose standalone or merged candidates

Europe PMC only:

```bash
python -m pmc_annotator.build_candidates build \
  --input "$RUN/annotations" \
  --out-candidates "$RUN/candidates.tsv" --out-mentions "$RUN/mentions.tsv"
```

Or combine both extractors:

```bash
python -m pmc_annotator.compare_extractions merge \
  --togoid "$REGEX" --epmc "$RUN/annotations" --output "$RUN/merged"

python -m pmc_annotator.build_candidates build \
  --input "$RUN/merged" \
  --out-candidates "$RUN/candidates.tsv" --out-mentions "$RUN/mentions.tsv"
```

Then pass candidates.tsv to your existing T2/T3 commands and mentions.tsv to
build_metrics, using separate verification output paths for each experiment.
Do not use --safe-only for Europe PMC imports: imported confidence is not high.
The merged Parquet retains source provenance; the legacy five-column mentions
TSV does not. Retain the Parquet for provenance and role windows.

## Coverage and interpretation

The starter mapping supports GEO series/samples, RefSeq subtypes, UniProt, Pfam,
and InterPro. It is intentionally not all Europe PMC databases. Extend
src/pmc_annotator/data/epmc_mapping.yaml or pass --mapping another.yaml. Each rule
must match BOTH the literal accession and provider URI. Multiple matching
database targets are rejected rather than guessed. No automated namespace
proposal is approved implicitly.

Only annotations with type Accession Numbers are imported. Linked gene names,
diseases and methods are excluded. Each imported occurrence must align uniquely
to a literal span in the local shared JATSParser output. Repeated text requires
prefix/suffix context to select one occurrence. Unaligned or ambiguous hits are
written to rejected.json, never assigned fake offsets. This excludes some valid
annotations in parser-omitted sections or differently formatted XML versions.

Check annotations/coverage.json, missing_papers.json and rejected.json before
interpreting the comparison. The comparison covers accepted aligned imports;
it is not a claim about all Europe PMC annotations. Missing caches, unavailable
annotations and mapping exclusions can all contribute to regex-only results.

comparison.tsv lists distinct (PMCID, internal database, literal accession)
triples as both, togoid_only or europepmc_only. by_database.tsv aggregates those
counts. summary.json reports totals. These are agreement counts, not precision
or recall against ground truth. Case and accession versions are preserved.

Merge deduplicates by (paper, database, literal accession, local offset), keeping
separate occurrences at different positions. It unions source labels and prefers
existing high confidence when present. Use fresh output directories; mixing old
shards with a new corpus can contaminate downstream counts.

The implementation was verified with offline fixtures. Live API availability
and your article-specific coverage must be checked on your sample.

## Test

```bash
python -m unittest discover -s tests -p 'test_epmc_layer.py'
```
