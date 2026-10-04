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
7. **Claude must not read, open, parse or run code against anything in `data/`, `outputs/` or `labels/`.** This holds until Optiv confirms whether case materials may be shared with AI tools; a question is pending. `.claude/settings.json` denies Read/Edit on these paths, but that does not stop scripts. So:
   - Never run the pipeline, tests or any script on files in those folders.
   - Never `cat`, `type`, `head` or print their contents.
   - Develop and test only against synthetic documents in `tests/fixtures/synthetic/` that reproduce the traps described in §4 by format, not by value.
   - Aryaman runs the pipeline on the real files himself and shares only aggregate results (counts, precision/recall) back. When a real-file run is needed, give him the exact command to run instead of running it.

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
- Image regions: OCR each image and map text back to pixel boxes for masking. Badge photos and barcodes are masked as whole regions.

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
- [ ] Extraction stage for all three formats into the block IR, with provenance and per-page OCR reporting (E.6 #1).
- [ ] Structure-retention metric.
- [ ] Rule detectors with validators, then GLiNER/NER, dictionary sweep, merge, allowlist.
- [ ] Tokenizer, redaction for text and each original format.
- [ ] Audit log and per-file summary.
- [ ] Ground-truth labels and evaluation (teammates may do the labelling).
- [ ] Demo notebook / UI.
- [ ] Deck (built separately, not in this repo).
