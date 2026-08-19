### EuropePMC TextMinedTerms vs TogoID: 相補性

- EPMC 抽出DB: **54**  /  TogoID dataset: **118**
- 共通: EPMC 26 DB ↔ TogoID 49 dataset
- EPMC独自: **28**  /  TogoID独自: **69**

| 区分 | EPMC独自 | 共通(EPMC) | TogoID独自 |
|---|---:|---:|---:|
| データ実体DB | 27 | — | 49 |
| オントロジー/用語 | 0 | — | 17 |
| 文献 | 0 | — | 2 |
| その他 | 1 | — | 1 |
| **計** | **28** | **26** | **69** |

#### TogoID独自 (EPMCが抽出していない) — 区分別
- **データ実体DB** (49): affy_probeset, atc, ccds, clinvar, cog, drugbank, flybase_gene, flybase_protein, flybase_transcript, gea, glycomotif, glytoucan, hmdb, homologene, inchi_key, iuphar_ligand, jga_dataset, jga_study, lipidmaps, lncbook_gene, lrg, mbgd_gene, mbgd_organism, mgi_allele, mgi_gene, mgi_genotype, mirbase, mirbase_mature, nbdc_human_db, ncbigene, oma_group, oma_protein, pathbank, prosite, pubchem_compound, pubchem_pathway, pubchem_substance, rgd, sgd, smart, swisslipids, tair, togovar, vgnc, wikipathways, wormbase_gene, xenbase_gene, zfin_gene, zfin_transcript
- **オントロジー/用語** (17): cl, clo, doid, ec, hp_inheritance, hp_phenotype, meddra, medgen, mesh, mondo, mp, nando, ncit_disease, ncit_tissue, sio, taxonomy, uberon
- **文献** (2): pmc, pubmed
- **その他** (1): prosite_prorule

**うち日本発資源**: gea, jga_dataset, jga_study, mbgd_gene, mbgd_organism, nando, nbdc_human_db, togovar

#### EPMC独自 (TogoIDが持たない) — 区分別
- **データ実体DB** (27): alphafold, arrayexpress, bia, biomodels, biostudies, brenda, cath, complexportal, dbgap, ebisc, ega, emdb, empiar, eudract, gisaid, gwas, hipsci, hpa, hPSCreg, igsr, metabolights, metagenomics, mint, nct, pxd, rrid, treefam
- **その他** (1): doi
