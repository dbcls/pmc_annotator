# PMC Identifier Role Annotation Guideline

_Draft reference for the `pmc_annotator` gold standard and for conventions to be
agreed with identifiers.org. As of 2026-10-27._

## 1. Purpose and scope

This guideline defines how to assign a single role — **generated**, **used**, or
**mentioned** — to each database-identifier occurrence found in PubMed Central
full text. It is the reference for annotating the gold standard and for agreeing
conventions with identifiers.org, so that an identifier co-occurrence can be
turned into an attributable deposition or reuse event.

One role is assigned per (document, dataset, identifier) occurrence, judged from
the surrounding text window alone — **context-only judgment**: the external
record and the author identity are not looked up. (Stricter resolution may
require that lookup; where it is unavailable, the context-only call is the one
recorded, with the reasoning noted.)

## 2. The three roles

- **generated** — the study produced this record and deposited or registered it.
  Credit goes to this paper's authors. Cues: Data Availability / Accession
  Numbers sections; "deposited", "we sequenced/submitted", "has been deposited".
- **used** — a pre-existing record, reference resource, or biological material is
  taken as input or material for this study. Credit goes to the original
  depositor. Includes: data downloaded / obtained / reanalysed; a reference
  genome aligned against; a sequence used to design primers / probes / siRNA or
  to build a construct; a numbering or variant reference; a strain / cell line /
  clone obtained and used.
- **mentioned** — the identifier only names, annotates, or cross-references an
  entity; the paper does not use its sequence or data in its own experiments.

## 3. Decision procedure

For each occurrence, in order:

1. Did this study deposit or register the record? → **generated**.
2. Is a pre-existing record or material taken as input or material
   (see Conventions A and B)? → **used**.
3. Otherwise — naming, annotation, cross-reference, or comparison listing —
   → **mentioned**.
4. If the occurrence cannot be placed on context alone, mark **unsure**
   (reserved for genuine ambiguity; see §6).

## 4. Convention A — the used / mentioned boundary (sequence / data)

The boundary is **not** "is the identifier written as a name?" — parenthetical
accessions are common on both sides. It is **whether the paper actually
processes the sequence or data**.

**An identifier is `used`** when the paper puts its sequence or data into its own
procedure:

- designed primers / probes / siRNA / shRNA / guides from it;
- cloned, sub-cloned, or built a construct / vector from it;
- aligned or mapped to it, or used it as a BLAST / search query;
- used it as a numbering or variant reference
  ("designations based on NM_198056.2");
- reanalysed the dataset it points to (e.g., a GEO series analysed in this
  study).

**An identifier is `mentioned`** when it appears only in:

- a naming or annotation statement ("X (NM_...) lacks an intron"; "the gene
  product of mRNA NM_...");
- a cross-reference pointer ("described in GEO sample GSM..."; "accession no.
  GSE..." with no analysis of that data here);
- a **comparison / homology listing** — a BLAST-hit table, a %-identity table,
  an ortholog / paralog list, a phylogenetic species list — where the window
  makes clear the accession only names a compared entity and its sequence is not
  processed here. If the window cannot exclude that the paper produced or
  processed the record, label **unsure** instead (§6).

The parenthetical form never decides the label. "GAPDH (NM_204305)
ggagtcaac..." used as a primer is **used**; "LAMA2 (NM_000426), lacking an
intron in mouse" is **mentioned**.

## 5. Convention B — physical-material acquisition

When something is obtained from a repository or company, decide by **what the
identifier denotes**:

- the identifier denotes the material that was obtained and then used — a strain,
  cell line, clone, or allele → **used**.
  Example: "BACE(−/−) mice (MGI:2389228) were obtained from Jackson Laboratory"
  → **used**.
- the obtained item is something else — an antibody, a physical structure, a kit
  — and the identifier only names the target entity → **mentioned**.
  Examples: "Creb3L2 (accession no. NP_919047) were obtained from Aviva Systems
  Biology" (antibodies obtained; the accession names the protein) →
  **mentioned**; "downloaded from PDB. GenBank identifiers: Rattus Kv1.1,
  NM_173095" (a structure obtained; the GenBank ID annotates the sequence) →
  **mentioned**.

## 6. Edge rules

- **"The data from those …" idiom → generated.** In context-only judgment,
  phrasing such as "The data from those experiments / samples …" attached to a
  deposited accession indicates the paper's own generated data, even when the
  surrounding text could read as reuse. Stricter resolution would require
  checking the external record and the author relationship; absent that,
  generated is the correct context-only call.
- **GEO deposition vs reuse.** A GEO series stated as "available in GEO (GSE...)"
  for data the paper itself produced is **generated**; the same form for a
  third-party dataset the paper analyses is **used**. Separate them with the
  deposition cues of §2.
- **Domain / family registries** (Pfam, InterPro, COG, SMART, TIGRFAM, ...) are
  almost always annotation → **mentioned**, unless the paper builds or searches a
  profile from them.
- **`unsure` is a conservative label**, assigned when the window does not allow
  `generated` or `used` to be excluded. When the identifier cannot be confirmed
  as naming-only, and the possibility that the paper produced or processed the
  record cannot be ruled out, withhold a definite label rather than defaulting to
  `mentioned` — this protects against under-crediting a deposition or reuse.
  Typical: dense comparison / homology tables (%-identity listings, BLAST-hit
  tables) where a listed accession could be the paper's own record or a processed
  input. Prefer a definite call only when the text positively supports one.
  (Known issue: the current classifier tends to push such cases toward
  `mentioned` — the under-crediting direction this label is meant to prevent;
  aligning its handling of unsure-type contexts is an open item, see §8.)

## 7. Worked examples

| Identifier (doc)          | Window (excerpt)                                               | Role                | Why                               |
| ------------------------- | -------------------------------------------------------------- | ------------------- | --------------------------------- |
| NM_198056.2 (PMC2822446)  | "amino acid designations were based on … transcript NM_198056.2" | used                | numbering reference (A)           |
| MI0000571 (PMC2377434)    | "miR-23a (MI0000571), 5′-GGAAAT…"                              | used                | sequence given as a primer (A)    |
| GSE6532 (PMC3098270)      | "In the GSE6532 dataset [22], tumors with poor prognosis …"    | used                | dataset reanalysed (A)            |
| MGI:2389228 (PMC3118598)  | "BACE(−/−) mice (MGI:2389228) were obtained from Jackson Lab."  | used                | strain obtained and used (B)      |
| NM_000426 (PMC2377436)    | "LAMA2 (NM_000426), lacking an intron in mouse"                | mentioned           | annotation statement (A)          |
| NP_919047 (PMC3003312)    | "Creb3L2 (accession no. NP_919047) were obtained from Aviva …" | mentioned           | antibody obtained; ID names target (B) |
| YP_881448 (PMC2667176)    | "YP_881471(478 aa) 90% YP_881448(446 aa) 94% …"                | unsure              | comparison table; window cannot exclude generated / used (§6) |

## 8. Status and open items

This guideline reflects the conventions used for the current gold standard
(221 items) after the used / mentioned refinement. Items to confirm with
identifiers.org:

- the deposition-vs-reuse language for GEO (generated vs used);
- whether comparison / homology listings should ever count as `used`
  (Convention A currently says no, and such windows are `unsure` when
  generated / used cannot be excluded);
- aligning the classifier's handling of `unsure`-type contexts (it currently
  leans `mentioned`, the under-crediting direction).
