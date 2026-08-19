# Repository reorganization — report

This describes how the uploaded scripts were sorted into a clean, installable layout, plus
**decisions you should confirm** before publishing. Nothing was deleted — anything I judged
superseded went to `archive/`, so it's all recoverable.

**Credential scan:** no hardcoded API keys / secrets found in any `.py` / `.json` / `.yaml`.
`.env` and `*.sqlite` are git-ignored.

## What moved where

| Destination | Files | Why |
|---|---|---|
| `src/pmc_annotator/` (kept) | preprocess, schema, io_utils, pipeline, phase1_ner, phase1_5_clean, phase_regex, phase_regex_togoid, regex_annotator, annotate_hunflair, **togoid_annotator**, launch_phase1, launch_phase3b, phase2_aggregate, phase3b_link, phase4_inject, build_windows, role_prepass, build_llm_payloads, role_llm_run, make_gold_sample, eval_roles | Canonical package (uses relative imports; must stay together) |
| `src/pmc_annotator/` (moved in from top level) | build_metrics, build_candidates, verify_t2, verify_t3, make_doc_year, rdfconfig_to_registry, build_togoid_patterns, digit_floor_gate, clean_noise_vocab | These are current pipeline steps and are **self-contained** (stdlib + duckdb/yaml/pandas only, no cross-imports), so moving them into the package is safe. They can now be run as `python -m pmc_annotator.<name>`. |
| `analysis/` | epmc_togoid_diff, scoped_eval, analyze_gene_precision, inspect_annotations | One-off analyses / evaluations, not part of the importable pipeline |
| `scripts/` | run_oa_pipeline | Orchestration runner |
| `tests/` | test_preprocess, test_pipeline, test_phase3b_phase4 (was under `src/`) | Genuine tests |
| `tests/diagnostics/` | annotate_hunflair, benchmark_throughput, diagnose_splitter, diagnose_xml_pipeline, verify_context_impact, verify_hunflair | Ad-hoc diagnostic scripts (not unit tests) |
| `archive/` | existence_verifier, existence_integration, unified_verifier, alliance_verifier (+ `.toplevel.py`), run_pilot_existence_check, check_mod_organism_keys, verify_against_pubtator, verify_against_pubtator_v2, togoid_extract_patterns_bak.yaml | Superseded — see decisions below |
| `archive/run-artifacts/` | kakuninn.txt, rdfportal_ep_verify.txt, report_table.md, phase*/summary.json | Scratch / run outputs, not source |
| `configs/`, `data/` | default.yaml; epmc_dbs.txt; `data/examples/shard_plan.example.json` | Config + small dictionaries + one shard_plan example |
| `docs/` | HANDOFF.md, HANDOFF_EN.md, REFERENCES.md, stage_b_setup.md | Documentation |
| (dropped) | `src/**/*.egg-info/` | Build artifacts; regenerated on install, now git-ignored |

## New scaffolding added
`pyproject.toml` (expanded deps + console-script entry points, built on your existing one),
`requirements.txt`, `.gitignore`, `.env.example`, `CITATION.cff`, `LICENSE` (placeholder),
and a layout/docs section appended to `README.md` (your original README content is preserved).

## ⚠ Decisions to confirm

1. **The archived existence stack.** `existence_verifier` / `existence_integration` /
   `unified_verifier` / `alliance_verifier` / `run_pilot_existence_check` / `check_mod_organism_keys`
   import each other (flat imports) and are imported by **nothing** in the current pipeline — they look
   superseded by `verify_t2.py` / `verify_t3.py`. I archived them. **Confirm they're dead** (then delete
   `archive/`), or tell me if any is still needed.
2. **`togoid_annotator.py` was NOT archived** even though the old stack imports it — because
   `phase_regex_togoid.py` (live) also imports it. It stays in the package. Confirm that's right.
3. **`alliance_verifier.py` had two different versions** (top-level vs `src/`). Both are archived;
   the top-level one is `archive/alliance_verifier.toplevel.py`. If one is canonical, keep that.
4. **`phase2_aggregate` / `phase3b_link` / `phase4_inject`** (concept-linking stages) are imported by
   nothing but look like part of the concept layer — I **kept them in the package**. Confirm they're
   still run; if not, move to `archive/`.
5. **`noise_vocab.json` is referenced** by `clean_noise_vocab.py` / `build_togoid_patterns.py` but was
   not in the upload (excluded as `.json`? or a data file). If it's config, add it under `data/`; if it's
   generated, document how.
6. **`LICENSE` is a placeholder.** Pick one (MIT suggested for the code) before publishing.
7. **`pyproject.toml` deps and entry points** are my best guess from imports — verify each declared
   dependency and that each `pmc-*` entry-point module has a `main()` before relying on them.
8. **`togoid_patterns_recall.yaml`** (the high-recall pattern file referenced in run commands) is **not**
   present — only `togoid_extract_patterns.yaml`. If recall-mode patterns are needed to reproduce, add
   that file (or document that `build_togoid_patterns.py --recall` regenerates it).

## Not included (by design)
Bulk data and outputs are git-ignored and excluded: OA XML, `*.parquet`, `*.sqlite`, the full
per-corpus `shard_plan.json` (~457 MB each), `usage_*.tsv`, `windows*.jsonl`, logs. Regenerate them by
running the pipeline stages documented in `docs/HANDOFF_EN.md`.

## Suggested next steps
1. `pip install -e .` in a fresh venv and confirm the package imports and entry points resolve.
2. Resolve decisions 1–8 above (mostly: confirm `archive/` is safe to delete).
3. Add a real `LICENSE`, fill `CITATION.cff` (`license`, repo URL), and set the GitHub repo URL.
4. Flesh out `tests/` (the highest-value regression is the JATSParser offset-alignment invariant used by the role stage).
