# CLAUDE.md — Cerberus

Context file for Claude Code. Read fully before working in this repo.

## 1. What this project is

Cerberus is a local-first pipeline that extracts text from PDF, DOCX and PPTX files (including scanned/image-only content via OCR), detects and classifies PII, and redacts it before any downstream LLM sees the content.

Immediate purpose: solution for Case Study 2 ("PII Detection within Text Artifacts") of the Optiv Consulting internship case-study round at VIT. Presentation and live demo on **14 October 2026**. Long-term purpose: a portfolio project, made public later with synthetic data only.

Owner and sole maintainer: Aryaman. It is a 5-person group submission, but only Aryaman commits code. Teammates may be added later.

Working style the owner wants:
- Explain design decisions from first principles. The owner must defend every choice in a live Q&A, so "why" matters as much as "what".
- Be brief but complete. No decorative output.
- Prefer simple, verifiable, deterministic components over clever ones.

## 2. Hard rules (confidentiality and safety)

1. **Never commit case material or anything derived from it.** That covers the original files, OCR output, extracted text, ground-truth labels, redacted outputs and audit logs. These live only in `data/`, `outputs/` and `labels/`, which are gitignored. Document and image extensions are also ignored globally; only `tests/fixtures/synthetic/**` and `docs/img/**` are exempt.
2. **No real PII values in committed files**, including this one, code comments, tests and commit messages. Describe formats (e.g. `EMP-#####`), never actual values from the case files.
3. **Detection runs locally.** The scenario's core constraint is that PII must not reach AI models, so no document content may be sent to any cloud API (OpenAI, Anthropic, Google, etc.) inside the pipeline. Local models only (spaCy, GLiNER, BERT-class NER, optionally a small local LLM).
4. Notebooks must have outputs stripped before commit (`pip install nbstripout` then `nbstripout --install` in the repo; verify it is active before the first notebook commit). Printed extracted text in a notebook cell is a leak.
5. Keep "Optiv" and "Cadence" out of code identifiers, module names and the README. Configs specific to the case can live in a gitignored local config. Before the repo goes public, this CLAUDE.md must be rewritten without case context.
6. The repo is private. It must not be made public before the recruitment process ends.
7. **Claude may read and run code against `data/`, `outputs/` and `labels/`** (Optiv confirmed AI-assisted development on 5 October 2026; Aryaman confirmed this covers Claude reading the case files directly). The limits:
   - Case data is only ever read locally. Rules 1–3 still apply: nothing from those folders is committed, and the pipeline itself sends no document content to any cloud API.
   - No real values in committed files, commit messages or `notes/`. Report results from real-file runs as counts, types and locations.
   - Anything committed as a demo or test fixture uses made-up values in `tests/fixtures/synthetic/`, reproducing the traps in §4 by format, not by value.
   - Pipeline outputs from real files go to `outputs/` only.

## 3. The brief (Case Study 2)

Scenario: "Cadence", a fictional global financial-services firm. Employees use a document-analysis agent (an LLM). A new policy forbids PII from being processed by AI models, so document content must be stripped of PII before it reaches the LLM.

Problem statement requirements:
- Extract text from each file format: scanned policy documents (PDF), organizational PowerPoint, process documents (DOCX).
- Identify and classify the PII, then redact it before LLM processing.
- PII detection must be **precise and consistent, with minimal false positives and no unidentified instances passed downstream**.
- The system must be **repeatable and scalable**: no hardcoding to the sample files.

Roles and responsibilities:
1. File structure analysis: analyse each sample file and justify an extraction approach per file type.
2. Develop the solution: extract text, detect and classify PII entities, apply **transparent tags**, **summarize what PII was found**, and **preserve source traceability** so findings stay explainable and repeatable.
3. Surface the exposure: report PII found (employee identifiers, full names, contact details, government-issued IDs, etc.) to support the internal audit process and keep PII from reaching the AI model.

Deliverables: a .pptx deck walked through in 10–15 minutes, covering:
1. Solution blueprint: a functional design diagram, from file ingestion to PII detection output.
2. System architecture: the technical components within each functional block and how they interact.
3. Extraction logic: the per-format extraction approach and the detection/classification logic.
4. Reliable outcomes:
   - Extracted text retains **no less than 80% of its original structure**, with preceding and succeeding context preserved.
   - PII detection approaches **100% coverage** (recall).
5. Live walkthrough: in Colab, Jupyter or a simple custom UI, upload the artifacts, extract text, and identify and classify the PII in each file. Show the redacted output too.

### 3.1 Hidden rubric: Section E.6 of the policy PDF (last page)

The scanned policy PDF ends with "Exercise prompts for students". Treat these as evaluation criteria:
1. Extract text from every page and report which pages required OCR.
2. Enumerate all PII instances by category, with page and location references, distinguishing **body text, tables, narrative prose and image-only** occurrences.
3. Identify every identifier that appears only inside an image, and **measure the recall difference** against digitally native text.
4. Locate identifiers embedded in unlabelled narrative prose (sections 16.5 and 16.8) and explain why field-based extraction fails there.
5. Produce a **masked derivative that preserves page count, layout and cross-artifact traceability of individuals via stable tokens**.
6. Justify the false-positive controls applied to **PAN, PESEL, TIN and card-number** patterns.

### 3.2 Our definition of "80% structure retained" (the brief does not define it)

Extraction produces an intermediate representation of ordered blocks: heading, paragraph, list item, table (rows, cells with header association), slide, text box, image region. Each block carries file, page/slide, bounding box and reading-order index.

Metric: of the structural elements in the source (headings, paragraphs, list items, table cells, slide titles, text boxes), the share present in the extraction with (a) correct order relative to neighbours and (b) correct parent (cell under the right header, text on the right slide/page). For OCR pages, also report character accuracy on a hand-checked sample of pages.

## 4. The sample files (in `data/`, never committed)

### 4.1 Scanned policy PDF (35 pages): the hard one
- Every page is a single image with no text layer, so the whole document needs OCR.
- Designed as a PII-detection test bed. It states that all data is synthetic and that identifier values "follow published test formats".
- PII appears in body text, tables, narrative prose and images:
  - Names; employee IDs `EMP-#####`; director IDs `DIR-####`; vendor staff IDs like `MER-PL-####`.
  - Corporate and external emails.
  - Phones in US, UK, Swedish and Polish formats.
  - Home addresses, dates of birth.
  - US SSNs, Indian PAN, Polish PESEL, passport numbers, tax IDs.
  - Card numbers and partial identifiers ("card ending ####", "last four digits of your SSN (####)").
- Specific traps:
  - **Narrative prose** with no field labels: sections 16.5 (p22) and 16.8 (p23).
  - p18, Figure 8: an embedded image of an attestation where one free-text column mixes DOBs, an SSN, a tax ID, a mobile number and a passport number.
  - p25: a vendor staff table with PAN and PESEL, plus international phone numbers.
  - p28: an **employee ID badge image** (photo, barcode, name, ID, email, mobile, DOB): image-only PII.
  - **Test-range values**: SSNs use the 900- prefix, which the SSA never issues. Strict validators would reject them; do not discard them.
  - **Already-masked values**: e.g. a PESEL shown as 6 digits followed by `xxxxx`. Decide and document the policy: flag as partial PII and still mask the visible part.
  - **OCR noise**: Tesseract at 200 dpi corrupted the email domain into near-variants (`cadencefy`, `cadencetg`) and dropped dots in some local-parts. Email/name matching must tolerate this.
- Section E.5 (p35) is a confidentiality/handling notice and E.6 holds the exercise prompts.

### 4.2 Organizational pack PPTX (9 slides)
- Leadership org charts, divisional charts, two RACI matrices, a role-holder directory table, and a closing data-handling notice (marked synthetic).
- 16 named role holders, each with a corporate email (a `.example` domain) and a mobile number, repeated across charts, tables and the directory.
- RACI tables use **abbreviated names** ("T. Surname"), so full-name dictionaries alone miss them.
- The org charts are separate positioned text boxes. python-pptx returns shapes in z-order, not reading order, so sort by position (top, then left) and group the boxes belonging to one card.
- The only image is a logo.

### 4.3 TPRM training DOCX (process document)
- A consulting deliverable documenting a OneTrust TPRM configuration: workflows, user groups, attributes, questionnaire rules, SLAs. About 400 paragraphs, 16 tables, 35 inline images.
- PII is **sparse**: user-group member names in a table, a client-contact name, document authors on a cover-page table, and a couple of names visible in screenshots.
- Its real test is **precision**. It is full of PII-looking non-PII: labels like "Primary vendor contact" and "Email ID of vendor POC", rule names, and product names (OneTrust, Ironclad, Torii, Oracle).
- Images are mostly low-resolution UI screenshots, and OCR produces icon noise. Several images are **EMF** (plus one SVG), which must be converted (LibreOffice or Inkscape) before OCR.
- Also read headers and footers, and the tables (python-docx `doc.paragraphs` alone misses table text).
- Unlike the other two files, it has no "synthetic data" disclaimer. Treat all names as real PII.

### 4.4 Cross-file relationships
- Same fictional company, but **not one consistent dataset**: different email domains (`.example` in the PPTX, `.com` in the PDF), and few overlapping people.
- One person appears in both files with a different role and contact details in each.
- There are **near-miss surnames** across files: same surname with different first names, and hyphenated variants of a surname that appears in another file.
- Cross-artifact token linking must therefore match on full normalized names (plus email/ID evidence), **never surname alone**.

### 4.5 Hidden channels (inspected 6 Oct 2026; record findings as types and counts, never values)
- **File metadata holds real names in all three files.** These are not synthetic: they are Optiv staff, with an "[OC]" prefix, and one of them is the evaluator who answered our questions.
  - DOCX: core-properties creator and last-modified-by (two different people), plus a last-printed date.
  - PPTX: creator and last-modified-by (one person).
  - PDF: Author, stored twice, in the info dictionary and in XMP `dc:creator`.
- DOCX `word/settings.xml` holds docVars with commercial tags (statement-of-work and pricing labels). Not PII, but scrub them in the masked derivative as general hygiene.
- PPTX picture alt text holds a leftover local file path from the tool that generated the deck. Not PII; scrub it.
- DOCX picture alt text is auto-generated ("A screenshot of a computer…"). No PII.
- PPTX speaker notes contain only slide numbers. No comments, tracked changes, hidden text or embedded objects in any file.
- The metadata names never appear in the visible text, so only the metadata channel exposes them. Never show the real names in the deck: show tokens.

## 5. Architecture decisions (agreed)

```
ingest → extract (per format) → block IR with provenance
       → detectors run in parallel on the ORIGINAL text:
           1. rules: regex + checksum/format validators + context keywords
           2. local NER: GLiNER (custom labels) and/or spaCy/BERT NER
           3. dictionary: names/emails/IDs found anywhere, swept across all files
           4. optional: small local LLM as verifier for ambiguous spans only
       → merge overlapping spans (union for recall) → allowlist filter (precision)
       → entity resolution → stable tokens (<PERSON_3>, <EMAIL_7>, ...) consistent across files
       → outputs: redacted text for the LLM, masked derivative in original format,
                  audit log, per-file summary report
```

Principles:
- **Detect in parallel, redact once.** Never redact sequentially: earlier redaction destroys the context later detectors need.
- **Every finding is traceable**: file, page/slide, block id, character offsets, bbox if available, location type (body/table/prose/image), entity type, detector(s) that fired, confidence, and the rule or model name. These are the "transparent tags".
- **Fail closed.** If a page or file cannot be parsed, or OCR confidence is below threshold, block it from downstream (quarantine and report it) rather than pass it through.
- **Generative LLMs never produce spans directly.** Any span proposed by a local LLM must be verified to exist verbatim in the source text before it is accepted.
- **Stable pseudonymization tokens**: the same individual gets the same token in every file, so the downstream LLM can still reason about who did what. The token→value mapping stays local and is never logged in committed files.
- **False-positive controls**: Luhn for card numbers; PESEL checksum and embedded-date validity; PAN structure (`AAAAA9999A`, 4th character = holder type, `P` for individuals); SSN structure, accepting the 900- test range but flagging it; context keywords for partial identifiers; an allowlist of org names, product names, job titles and field labels.
- Image regions: OCR each image and map text back to pixel boxes. **Every figure is masked as a whole region** in the masked derivative (decided 6 Oct 2026), and text read inside a figure is used for detection and the audit report only, never passed downstream. Reason: figure OCR is too noisy to trust span-level masking. Revisit if a neural OCR engine is adopted for figures.
- **Masked derivatives rewrite metadata.** DOCX/PPTX: core properties set to tokens or cleared. PDF: info dictionary cleared, XMP deleted, saved non-incrementally with `garbage=4` (an incremental save appends changes and leaves the original objects recoverable). Extractors emit metadata as blocks so it is detected like any other text.
- **Verification pass.** Every output (redacted text and each masked derivative, metadata included) is re-extracted and re-scanned after redaction. If any PII remains, the output is withheld and the run fails: fail closed. The pass has two independent checks, both run by the pipeline itself: (1) re-run extraction and the detectors on the output; (2) search the output's raw contents (every part inside a DOCX/PPTX zip, every decoded PDF stream, plus metadata) for each original value held in the local token map. Check 2 catches a value left in a part of the file the extractor does not walk. Tokens such as `<PERSON_3>` are recognised and not flagged.

Suggested stack (confirm before adding dependencies):
- Python 3.11.
- PDF: PyMuPDF (render, redact, rebuild preserving page count).
- OCR: Tesseract via pytesseract. Evaluate PaddleOCR or docTR if table/layout quality is poor.
- DOCX: python-docx. PPTX: python-pptx.
- EMF conversion: LibreOffice headless.
- Detection: Microsoft Presidio (analyzer + custom recognizers), spaCy, GLiNER PII model.
- UI: a Colab/Jupyter notebook and optionally Streamlit or Gradio.
- The demo must run without a GPU; any local LLM is optional.

## 6. Repo layout

```
cerberus/
  cerberus/
    extract/     # pdf.py, docx.py, pptx.py, ocr.py, ir.py (block representation)
    detect/      # rules/, ner.py, dictionary.py, merge.py, allowlist.py
    redact/      # tokens.py, text.py, pdf.py, docx.py, pptx.py
    report/      # audit log, per-file summary, exposure report
    eval/        # ground-truth loader, precision/recall, structure metric
  notebooks/demo.ipynb
  tests/fixtures/synthetic/   # our own generated documents (Faker), safe to commit
  data/        # gitignored: case files
  outputs/     # gitignored: all generated outputs
  labels/      # gitignored: hand-labelled ground truth
```

## 7. Evaluation plan

- Hand-label every PII instance in the three files to `labels/` (gitignored), with type, file, page/slide, location type and exact text.
- Report precision, recall and F1 per detector, per file, per entity type, and per location type (body/table/prose/image). This answers E.6 #2 and #3 and the "~100% coverage" claim.
- Show an ablation: regex only → + NER → + dictionary → + LLM verifier.
- Build a synthetic document generator (Faker) with known PII positions for regression tests and the eventual public demo.

## 8. Open questions sent to Optiv (assume as noted until answered)

| Question | Assumption until answered |
|---|---|
| Unseen documents at the demo? | Yes. Stay generic. |
| How is 80% structure measured? | Our metric in §3.2. |
| Redacted text or masked original format? | Both. |
| Are the E.6 prompts graded? | Yes. Address all six. |
| Are work contact details, partial IDs and already-masked values PII? | Yes, all three, tagged with sub-type. |
| Must image photos/barcodes be masked? | Yes, whole region. |
| Are local models allowed? | Yes. |

## 9. Current status and next steps

- [x] Repo created (private), .gitignore in place, cloned locally.
- [x] nbstripout installed and active; folder skeleton created.
- [x] Block IR; PPTX and DOCX extractors (native text, tables, hidden text; images recorded, not yet OCR'd).
- [x] PDF extractor: per-page native/OCR routing and OCR report (E.6 #1), layout analysis (headings, paragraphs, lists, tables from word positions), running headers/footers, metadata.
- [x] PDF figures: framed figures detected from pixels (13 of 13 in the sample), cut out at native scan resolution, enlarged and re-OCR'd as `image_region` blocks with per-line child blocks (container = image). A figure below the confidence threshold is quarantined (`Block.quarantined`): mask whole, keep from downstream.
- [ ] Figure OCR quality: Tesseract cannot read 3 of the 13 sample figures (two UI/terminal screenshots and a chart) and reads the rest with noise. Decided: mask every figure whole for now. Later, evaluate a neural OCR engine (PaddleOCR or docTR) for figures only, to improve the audit report and image-only recall.
- [x] PDF tables: pixel evidence added. A dark header band plus ruled lines marks out the table and its rows; single-row strips are merged and bridged columns split. On the sample, 59 of 67 detected tables have a clean header row.
- [ ] PDF tables, remaining: tables continuing across a page break (no header band on the second page), two adjacent prose columns merged, cell text on coloured badges not read by OCR.
- [x] OCR for pictures in PPTX, DOCX and typed PDF pages (`images.py`): read once per distinct picture, EMF/WMF/SVG converted through LibreOffice, enlargement chosen from measured text height, lines emitted as image content. Unopenable or low-confidence pictures are quarantined. Extraction now covers every channel in all three formats.
- [x] Structure-retention metric (`cerberus/eval/structure.py`): scores extraction against hand-corrected reference files in `labels/structure/` (a small Markdown dialect, one element per line). Export a page, correct it by eye, delete the word UNCHECKED, score. Reports the retained share, the reasons for losses, and character accuracy; text inside pictures is scored separately.
- [ ] Structure references: a person must correct exported pages for a sample of each file (suggest 8 to 10 PDF pages including 18, 22, 25, 28 and 30; 3 slides; 2 DOCX sections). Only page 25 exists so far, checked by Claude as a tool demonstration: 95.2% retained, 99.9% character accuracy.
- [x] Rule detector (`cerberus/detect/rules`): e-mail, phone, staff ID, SSN (full and last-four), tax ID, card (full and last-four), PAN, PESEL (full and already-masked), passport, date of birth. Validators: Luhn, PESEL checksum and date, PAN holder letter, SSN structure with the 900 range accepted and flagged. Staff-ID prefixes are learned from column headers and from codes that follow a name, not hardcoded. `Finding` carries the transparent tags; location is body / table / prose / image.
- [x] NER for names and addresses (`cerberus/detect/ner.py`): GLiNER, model `knowledgator/gliner-pii-small-v1.0` in the gitignored `models/` folder, loaded offline. Table rows and file properties are given their labels as context. Threshold 0.2 (low on purpose), then the shape and word filters in `allowlist.py` restore precision. "organization" and "job title" are offered to the model as distractor labels and discarded.
- [x] Dictionary sweep (`cerberus/detect/dictionary.py`): names found anywhere are searched for in every file, in their usual written forms (without middle initial, abbreviated, reversed), approximately in OCR text. Surnames and first names alone are not swept. Names from pictures and from OCR headers/footers do not seed it. `cerberus.detect.detect()` runs rules, NER and the sweep on the original text and settles overlaps once.
- [x] False-positive gates: a name seeds the sweep only with independent evidence (an e-mail carrying its surname, a staff ID in the same block or row, a person-type column header, an author file property, or two sightings); names on low-confidence OCR words cannot seed. Addresses must contain a street line or a postcode. Optional review file (`detect(..., review=path)`): written on first run with the pipeline's keep/drop decision and evidence per name; a person's edits are applied on the next run. On the sample: 104 names kept, 11 held back (all real, single mentions), at a cost of 3 missed mentions in the DOCX.
- [ ] Detection, remaining: allowlist for organisation contact details (a switchboard number, a letterhead address), single-name mentions (a first name alone), names in all capitals with a middle initial, and calibration of thresholds once labels exist.
- [ ] Set-up note for a new machine: install CPU torch, `pip install -r requirements.txt`, then download the model once into `models/gliner-pii-small-v1.0` (about 320 MB). Nothing is fetched at run time.
- [x] Entity resolution and stable tokens (`cerberus/redact/tokens.py`): values are compared after normalising how they are written; people by full name, with written forms resolving to one person, a surname alone never linking, and an abbreviation linked only if exactly one person fits. A saved vault (`outputs/vault.json`, holds original values) keeps numbering stable across runs. `image_only()` lists identities seen only inside pictures (E.6 #3). On the sample: 1,104 findings resolve to 300 identities, 100 of them people; 2 people appear in more than one file; 41 identities appear only in pictures.
- [ ] Redaction for text and each original format, including metadata rewrite.
- [ ] Verification pass: re-scan every output including metadata, plus raw-content search for token-map values; fail closed on any residual PII.
- [ ] Audit log and per-file summary.
- [x] PII evaluation tool (`cerberus/eval/pii.py`): writes one CSV per document to `labels/pii/` with a row per finding; a person marks each `ok` or `wrong` and adds `missed` rows; `score` reports precision, recall and F1 overall, by entity and by location. Re-exporting keeps verdicts already given.
- [ ] PII labelling by the team: mark the three sheets in `labels/pii/` (927, 124 and 53 rows) and add the misses by reading each document. Nothing is marked yet, so there is no precision or recall figure.
- [ ] Demo notebook / UI.
- [ ] Deck (built separately, not in this repo).
