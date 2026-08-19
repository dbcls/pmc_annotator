# Related Work & Resources

Reference list and gap analysis for the PMC data-reference extraction project. Each entry notes **how it
differs from this project** and **our adoption plan**. Citation details should be double-checked against the
originals before external circulation.

---

## Comparison table

### A. Literature → data / accession mention extraction

| # | Resource / effort | What it is | Difference vs. this project | Adoption plan |
|---|---|---|---|---|
| [1][2][3] | **Europe PMC** text-mining (Annotations API, SciLite, annotated corpus) | EBI's text-mined accession/entity annotations over EPMC full text; `TextMinedTerms` (≈54 DBs) published weekly | Broad and authoritative, but **mention-level only** — no entry-level existence verification and no created/used/mentioned role | **Ingest `TextMinedTerms` as a baseline input**; layer verification + role classification on top; quantify complementary coverage (`epmc_togoid_diff.py`) |
| [4] | **CZI** dataset / software mention extraction | SciBERT-based NER over ~20M papers (PMC-OA + publisher corpus); feeds the Data Citation Corpus | ML-NER vs. our regex + TogoID + verification; no existence check, no role typing | Closest methodological peer; compare recall on shared DBs; consider their outputs as a candidate source |
| [5] | **Coleridge "Show US the Data"** | Benchmark corpus (~14.3k papers, ~35k dataset mentions) + detection/normalization methods | Focus on mention detection & string→dataset linking; no deposit-vs-reuse role | Candidate train/eval data for the role classifier and for normalization; benchmark our gold set against it |
| [6] | **LLM-based dataset reference extraction** (SDP 2025) | Recent LLM approach to extracting dataset references from papers | Same LLM direction as our role classifier | Track prompt/schema techniques; compare against our rule + LLM hybrid |
| [7] | **ICPSR informal-reference NLP pipeline** | NER + section-aware pipeline for *informal* (un-cited) data references | Explicitly notes un-cited references require **inference** of intent | Validates our created/used/mentioned inference framing; adopt section-aware signals; cite as motivation |
| [8] | **PubTator3 / PubTator Central** (NCBI) | Full-text concept annotation & entity linking at scale | Concept-layer peer to our NER stage | Benchmark / alternative for the concept layer [9] |

### B. Data-citation corpora, metrics & standards

| # | Resource / effort | What it is | Difference vs. this project | Adoption plan |
|---|---|---|---|---|
| [10] | **Data Citation Corpus** (DataCite × Make Data Count) | Central open corpus of data citations (DOIs + accessions); v4 (Jul 2025) ingested 5.2M citations from EPMC | Aggregates mentions; **no per-entry existence verification, no role typing** | Evaluate against it (`scoped_eval.py`); position as an upstream contributor of *verified, role-typed* entries |
| [11] | **Make Data Count** | Community initiative for open data-usage/citation metrics | The metrics ecosystem we plug into | Align output conventions; potential contribution channel |
| [12] | **FORCE11 "Data Usage Typologies" WG** | Community effort to standardize a typology of data *uses* | **Directly parallels our created/used/mentioned roles** | Map our role scheme to the emerging typology; contribute our empirical role distribution as evidence |
| [13] | **COUNTER Code of Practice for Research Data** | Standard for repository views/downloads usage metrics | Repository-side usage signal (complementary to our citation-side signal) | Keep our metric interoperable; cite as the usage-metric counterpart |
| [14] | **Scholix / Scholexplorer** | Framework to exchange data–literature links (DOI-centric today) | Accession (non-DOI) links are exactly the gap Scholix leaves open | Consider exposing verified links in a Scholix-compatible form |
| [15] | **GREI** (NIH Generalist Repository Ecosystem Initiative) | Standardized usage counts across generalist repositories | Repository-side ecosystem | Context / positioning |
| [16] | **FORCE11 Data Citation Principles** | Normative principles for data citation | Foundational norms | Cite as framing |

### C. Identifier registries & reconciliation (EBI / Identifiers.org core area)

| # | Resource / effort | What it is | Difference vs. this project | Adoption plan |
|---|---|---|---|---|
| [17] | **Identifiers.org / MIRIAM Registry** | Perennial URI resolution for life-science records (ELIXIR) | **Resolution** (CURIE→provider) is *complementary* to our **existence verification** (does the entry actually exist) | Use as a resolution/verification route; reconcile prefixes; **primary collaboration surface** |
| [18] | **Bioregistry** (biopragmatics) | Open metaregistry aligning 23 registries incl. Identifiers.org & Prefix Commons; CURIE parsing/resolution (MIT/CC0) | Reference implementation for prefix/CURIE reconciliation | Adopt as the canonical prefix-map backbone; align TogoID ↔ Identifiers.org ↔ RDF Portal via Bioregistry |
| [19] | **TogoID** (DBCLS) | Ontology-backed ID-conversion service/API across datasets | Our identifier backbone (extraction patterns + verification) | Core dependency; reconcile with Identifiers.org / Bioregistry |

### D. Impact / policy framing

| # | Resource / effort | What it is | Difference vs. this project | Adoption plan |
|---|---|---|---|---|
| [20] | **NIH Data Sharing Index (S-index)** | Per-researcher data-sharing index (NEI challenge) | Different granularity (researcher, not entry) — complementary | Supply the reuse-evidence base; position as complementary |
| [21] | **Global Biodata Coalition — Global Core Biodata Resources** | Sustainability designation for core data resources | Framing for resource importance/sustainability | Cite for positioning |
| [22] | **FAIR Guiding Principles** | Foundational data-stewardship principles | Foundational | Cite as framing |

*Tools this project builds on directly: HunFlair2 [23] (concept layer) and TogoID [19] (identifier layer).*

---

## References

[1] Yang X, Saha S, Venkatesan A, Tirunagari S, Vartak V, McEntyre J. **Europe PMC annotated full-text corpus for gene/proteins, diseases and organisms.** *Scientific Data* 10:687 (2023). doi:10.1038/s41597-023-02617-x.

[2] Europe PMC. **Annotations API** and **RESTful Web Service.** https://europepmc.org/AnnotationsApi ; https://europepmc.org/RestfulWebService (accessed 2026).

[3] Kafkas Ş, et al. **Database citation / text-mined accessions in Europe PMC full text** (Europe PMC text-mining method line; see also "Section-level search functionality in Europe PMC"). *Verify exact citation before use.*

[4] Istrate A-M, Li D, Taraborelli D, Torkar M, Veytsman B, Williams I. **A large dataset of software mentions in the biomedical literature.** arXiv:2209.00693 (2022). (Chan Zuckerberg Initiative)

[5] Coleridge Initiative. **"Show US the Data"** dataset-mention benchmark (~14.3k papers / ~35k mentions) and associated dataset-mention extraction/classification methods (Kaggle, 2021; and follow-on NLP papers).

[6] **LLM-Powered Dataset Reference Extraction from Scientific Publications.** Proceedings of the Scholarly Document Processing Workshop (SDP), ACL (2025). https://aclanthology.org/2025.sdp-1.10/.

[7] **A Natural Language Processing Pipeline for Detecting Informal Data References in Academic Literature.** arXiv:2205.11651 (2022). (ICPSR)

[8] Wei C-H, Allot A, Leaman R, Lu Z. **PubTator Central: automated concept annotation for biomedical full text articles.** *Nucleic Acids Research* 47(W1):W587–W593 (2019). (See also PubTator 3.0, NAR 2024.)

[9] Sänger M, Garda S, Wang XD, Weber-Genzel L, Droop P, Fuchs B, Akbik A, Leser U. **HunFlair2 in a cross-corpus evaluation of biomedical named entity recognition and normalization tools.** *Bioinformatics* 40(10):btae564 (2024). doi:10.1093/bioinformatics/btae564.

[10] DataCite & Make Data Count. **Data Citation Corpus** (Release 4.0, 27 Jul 2025; incorporates 5.2M citations text-mined by Europe PMC). Dashboard: https://corpus.datacite.org/dashboard ; data file: Zenodo doi:10.5281/zenodo.11196858.

[11] Make Data Count. https://makedatacount.org (partners: CDL, Crossref, DataCite, DataONE, ScholCommLab, Univ. Ottawa, ZBW).

[12] FORCE11 **Data Usage Typologies** Working Group (co-led by Make Data Count, 2025).

[13] **COUNTER Code of Practice for Research Data.** Project COUNTER. https://www.projectcounter.org/ (research-data usage metrics; SUSHI reporting to DataCite).

[14] Burton A, et al. **The Scholix framework for interoperability in data–literature information exchange.** *D-Lib Magazine* 23(1/2) (2017). Scholexplorer: https://scholexplorer.openaire.eu/.

[15] NIH **Generalist Repository Ecosystem Initiative (GREI).** https://datascience.nih.gov/data-ecosystem/generalist-repository-ecosystem-initiative.

[16] Data Citation Synthesis Group. **Joint Declaration of Data Citation Principles.** FORCE11 (2014). doi:10.25490/a97f-egyk.

[17] Juty N, Le Novère N, Laibe C. **Identifiers.org and MIRIAM Registry: community resources to provide persistent identification.** *Nucleic Acids Research* 40(D1):D580–D586 (2012). https://identifiers.org.

[18] Hoyt CT, Balk M, Callahan TJ, Domingo-Fernández D, Haendel MA, Hegde HB, Himmelstein DS, Karis K, Kunze J, Lubiana T, Matentzoglu N, McMurry J, Moxon S, Mungall CJ, Rutz A, Unni DR, Willighagen E, Winston D, Gyori BM. **Unifying the identification of biomedical entities with the Bioregistry.** *Scientific Data* 9:714 (2022). doi:10.1038/s41597-022-01807-3. https://bioregistry.io ; https://github.com/biopragmatics/bioregistry.

[19] Ikeda S, Ono H, Ohta T, Chiba H, Naito Y, Moriya Y, Kawashima S, Yamamoto Y, Okamoto S, Goto S, Katayama T. **TogoID: an exploratory ID converter to bridge biological datasets.** *Bioinformatics* 38(17):4194–4199 (2022). doi:10.1093/bioinformatics/btac491. https://togoid.dbcls.jp.

[20] NIH National Eye Institute. **Data Sharing Index (S-index) Challenge.** (See NIH/NEI challenge pages.)

[21] Global Biodata Coalition. **Global Core Biodata Resources.** https://globalbiodata.org.

[22] Wilkinson MD, et al. **The FAIR Guiding Principles for scientific data management and stewardship.** *Scientific Data* 3:160018 (2016). doi:10.1038/sdata.2016.18.

[23] (HunFlair2 — see [9].)
