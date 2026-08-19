# pmc-annotator

Extract life-science database identifiers from **PubMed Central (PMC)** full text, **verify that each
referenced entry actually exists**, and **classify the role of each reference** — `created` (the paper
deposited the data), `used` (existing data/resource reused), or `mentioned` — to build a data-usage /
data-reuse metric.

> **Status:** runs end-to-end on a feasibility subset (~6.4% of the corpus, ~17.5k papers). Full-corpus
> scale-up and the human evaluation of the role classifier are in progress.
> A fuller introduction for external collaborators is in [`docs/HANDOFF_EN.md`](docs/HANDOFF_EN.md);
> related work and positioning in [`docs/REFERENCES.md`](docs/REFERENCES.md).
> 日本語版は [README.ja.md](README.ja.md)。

## Pipeline

```
PMC OA full-text XML
  ├─ concept layer   : HunFlair2 NER (gene/chemical/disease/species/cell_line)      [GPU]
  └─ identifier layer: regex + TogoID patterns  (GEO/SRA/RefSeq/UniProt/PDB/...)
        ↓ 3-layer gate  (digit-floor + context gate + existence verification)
        ↓ existence verification
             ├─ T2: TogoID label graph / rdf-config native URI  (RDF Portal SPARQL)
             └─ T3: NCBI E-utilities (RefSeq/GEO; can assert absence)
        ↓ metrics        (usage_by_entry/db/doc/long, DB_CLASS, year distribution)
        ↓ role classification
             build_windows → role_prepass (rules) → build_llm_payloads → role_llm_run (LLM)
        ↓ usage_roles.tsv  (created / used / mentioned)  →  evaluation (gold + confusion matrix)
```

Each stage is **idempotent per shard** (`.done` markers); a failed shard can be re-run on its own.

## Repository layout

```
src/pmc_annotator/   installable package (extraction → verification → metrics → role classification)
analysis/            one-off analyses & evaluations (EPMC/TogoID coverage diff, Data Citation Corpus eval)
scripts/             orchestration runners (run_oa_pipeline.py)
tests/               unit tests (+ tests/diagnostics/ for ad-hoc diagnostic scripts)
archive/             superseded modules — kept for reference, NOT wired into the pipeline (see REORG_REPORT.md)
configs/, data/      config + small dictionaries + a shard_plan example (bulk data is git-ignored)
docs/                HANDOFF (EN/JA), REFERENCES, stage-B setup
```

## Install

```bash
pip install -e .              # core stages (identifier + verification + metrics + role)
pip install -e ".[hunflair]"  # + concept layer (HunFlair2; GPU box)
cp .env.example .env          # then fill NCBI_API_KEY / Azure creds if used
```

## Quickstart

**Concept layer (Stage A preprocess + Stage B HunFlair2):**
```bash
python -m pmc_annotator.pipeline all \
    --input /path/to/PMC_OA_xml --intermediate ./data/intermediate --output ./data/output
```

**Identifier layer → verification → metrics:**
```bash
python -m pmc_annotator.phase_regex_togoid run \
    --plan ./data/oa/phase1/shard_plan.json --output <out> \
    --patterns src/pmc_annotator/data/togoid_extract_patterns.yaml \
    --global-range 0:1000 --workers 64
python -m pmc_annotator.verify_t2 ...      # TogoID label graph / RDF Portal
python -m pmc_annotator.verify_t3 ...      # NCBI E-utilities
python -m pmc_annotator.build_metrics ...  # usage_by_*, DB_CLASS
```

**Role classification:**
```bash
python -m pmc_annotator.build_windows \
    --usage metrics/usage_long.tsv --parquet-dir <togoid parquet dir> \
    --xml-root ~/PMC_xml --out windows.jsonl
python -m pmc_annotator.role_prepass       --windows windows.jsonl --out role_prelim.tsv
python -m pmc_annotator.build_llm_payloads --prelim role_prelim.tsv --windows windows.jsonl \
    --out llm_payloads.jsonl --max-entities-per-doc 15
# start a local OpenAI-compatible server (see below), then:
python -m pmc_annotator.role_llm_run \
    --payloads llm_payloads.jsonl --prelim role_prelim.tsv --out usage_roles.tsv \
    --backend oss --base-url http://localhost:8001 --base-url http://localhost:8002 \
    --model qwen3.6-27b --json-mode schema --concurrency 24
```

**Evaluation:**
```bash
python -m pmc_annotator.make_gold_sample --usage-roles usage_roles.tsv --windows windows.jsonl \
    --n 300 --min-per-cell 20 --cap-per-cell 60      # -> gold_todo.tsv (label it) + gold_key.tsv
python -m pmc_annotator.eval_roles --todo gold_todo.tsv --key gold_key.tsv
```

Console entry points (`pmc-build-windows`, `pmc-role-prepass`, `pmc-role-run`, `pmc-make-gold`,
`pmc-eval-roles`, `pmc-annotate`) are installed by `pip install -e .`.

## LLM serving (role classification)

Reference setup: 2× NVIDIA L40S (46 GB), model **`Qwen/Qwen3.6-27B-FP8`**, one replica per GPU
(data-parallel). vLLM must run with **thinking disabled** (`chat_template_kwargs.enable_thinking=false`,
`--trust-remote-code`), **temperature ≠ 0** (Qwen3 loops at 0), and **`--max-model-len 16384`**
(accession-dense windows tokenize above an 8192 budget). Azure OpenAI works as a fallback
(`--backend azure`). Details in [`docs/HANDOFF_EN.md`](docs/HANDOFF_EN.md).

## Operational notes (full corpus)

- **Shard parallelism:** with N GPUs, split shards into N ranges and run one worker each with
  `--device cuda:N`. Assign shard-ID ranges per worker so `.done` markers don't collide.
- **Job queue:** to use Celery / RQ / Airflow, decompose the per-shard loop into one task per shard.
- **Intermediate storage:** consolidating to Parquet enables cross-shard queries from DuckDB/ClickHouse.
  For RDF/SPARQL output, add a BioC-JSON → Turtle converter.
- **Incremental updates:** PMC updates daily; run preprocessing on the delta into a separate shard
  namespace so existing shards are untouched.

## Extending the annotators

Concept/dictionary/regex annotators share one interface — `__call__(Document) -> Document` (see
`HunFlairAnnotator`). Implement a new annotator and apply it in `pipeline.py`; the `Annotation.source`
field records provenance, so downstream code can weight or de-duplicate by source.

## Documentation
- [`docs/HANDOFF_EN.md`](docs/HANDOFF_EN.md) — project introduction (external collaborators).
- [`docs/HANDOFF.md`](docs/HANDOFF.md) — same, Japanese.
- [`docs/REFERENCES.md`](docs/REFERENCES.md) — related work + comparison table.
- [`docs/stage_b_setup.md`](docs/stage_b_setup.md) — HunFlair2 (GPU) setup.
- [`REORG_REPORT.md`](REORG_REPORT.md) — repository cleanup report + open decisions.

## Citation & license
See [`CITATION.cff`](CITATION.cff). This project builds on **TogoID** (Ikeda et al., *Bioinformatics*
2022, doi:10.1093/bioinformatics/btac491) and **HunFlair2** (Sänger et al., *Bioinformatics* 2024,
doi:10.1093/bioinformatics/btae564). License: see [`LICENSE`](LICENSE) *(TODO — choose before publishing)*.
