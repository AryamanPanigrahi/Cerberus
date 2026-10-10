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
           2. local NER: GLiNER (custom labels)
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
- Image regions: every picture is read by two OCR engines when the second is installed (Tesseract and docTR), each reading kept as its own lines so detection sees both. Text read inside a picture is used for detection and the audit report only, never passed downstream. **A picture is masked word by word only if the two engines agree on at least half of its text; otherwise it is masked whole** (decided 8 Oct 2026, replacing the 6 Oct decision to mask every figure whole). Inside a trusted picture a word is marked if it is part of a finding; part of anything shaped like personal data (name-like runs, initials plus surname, "@", a word with five or more digits, seven or more digits in groups, written dates); a capitalised word the document never writes in small letters (a surname alone in a cell); or a word the engines read differently where neither read an ordinary word. "Ordinary" means a word the document itself writes in small letters in reliable text. What is covered is wider than what is marked (9 Oct, after a review found a name left visible on page 18): **a table cell or label's value is covered whole if any word in it is marked** (cells are found from gaps and from column starts shared by three or more lines); in a sentence, neighbouring marked words are one rectangle; initials beside a rectangle are taken into it; every rectangle gets a margin. Short reference codes and years are not marked. Faces and barcodes are covered. No token is written inside a picture. After masking, the picture is read again: if a detector still finds PII, or initials stand beside a mask, or a mask cuts through a word, that picture is masked whole. **Every picture masked whole is listed in `report/review_queue.csv` with its reason**: the pictures a person must look at. `--pictures whole` masks them all; `CERBERUS_OCR=tesseract` (fast mode) reads with one engine, which also masks them all.
- **Masked derivatives rewrite metadata.** DOCX/PPTX: core properties set to tokens or cleared. PDF: info dictionary cleared, XMP deleted, saved non-incrementally with `garbage=4` (an incremental save appends changes and leaves the original objects recoverable). Extractors emit metadata as blocks so it is detected like any other text.
- **Verification pass.** Every output (redacted text and each masked derivative, metadata included) is re-extracted and re-scanned after redaction. If any PII remains, the output is withheld and the run fails: fail closed. The pass has two independent checks, both run by the pipeline itself: (1) re-run extraction and the detectors on the output; (2) search the output's raw contents (every part inside a DOCX/PPTX zip, every decoded PDF stream, plus metadata) for each original value held in the local token map. Check 2 catches a value left in a part of the file the extractor does not walk. Tokens such as `<PERSON_3>` are recognised and not flagged.

Suggested stack (confirm before adding dependencies):
- Python 3.13 (the environment this is built and tested in; `requirements.lock` holds the exact versions).
- PDF: PyMuPDF (render, redact, rebuild preserving page count).
- OCR: Tesseract via pytesseract for pages; pictures also by docTR (`db_resnet50` + `crnn_vgg16_bn`, CPU, weights in `models/doctr`). Faces: OpenCV YuNet (`models/opencv`). Barcodes: pyzbar, OpenCV and a pattern check.
- DOCX: python-docx. PPTX: python-pptx.
- EMF conversion: LibreOffice headless.
- Detection: our own rule detector with validators, and GLiNER (`knowledgator/gliner-pii-small-v1.0`) for names and addresses. Presidio and spaCy were considered at the start and are not used.
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
  tests/fixtures/synthetic/   # kept for generated documents; they are written at test time by eval/synthetic.py
  data/        # gitignored: case files
  outputs/     # gitignored: all generated outputs
  labels/      # gitignored: hand-labelled ground truth
```

## 7. Evaluation plan

- Hand-label every PII instance in the three files to `labels/` (gitignored), with type, file, page/slide, location type and exact text.
- Report precision, recall and F1 per detector, per file, per entity type, and per location type (body/table/prose/image). This answers E.6 #2 and #3 and the "~100% coverage" claim.
- Show an ablation: regex only → + NER → + dictionary → + LLM verifier.
- A synthetic document generator with known PII (`cerberus/eval/synthetic.py`, hand-written values, no Faker) is used for regression tests and the demo.

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
- [x] Second OCR engine for pictures (`cerberus/extract/neural.py`, `regions.py`; `images.examine`): docTR on overlapping 1024-pixel tiles of the enlarged picture (text height target 28 px, at most 4x and 8 tiles), union with Tesseract, agreement score per picture, doubtful words, faces and barcodes recorded on the image region (`native_ref`: engines, agreement, trusted, cover). Duplicate findings from the two engines are reported once (`detect/merge.one_per_place`, flag `two_engines`). Readings are cached under `<out>/cache`. `CERBERUS_OCR=tesseract` switches the second engine off.
- [x] Word-level masking inside trusted pictures, in all three writers (`redact/office.picture_boxes`); DOCX/PPTX pictures are rebuilt from pixels as PNG. Fallback to whole on low agreement, on a failed re-scan (`pipeline.distrust_pictures`) or with `--pictures whole`. On the sample (9 Oct, after narrowing the covering rules): both verification checks passed on all six outputs; 1,139 findings, 316 identities; pictures masked word by word / whole: PDF 8 / 5 of 13 (pages 6 and 19 fell back after the re-scan), DOCX 24 / 21 of 45, PPTX 9 / 0 of 9. The fallback is per picture (per page on a PDF). A first run takes about 50 minutes; a repeat run with cached readings about 13. Release now refuses cleanly if an earlier output is open in a viewer.
- [ ] Image recall, measured 8 Oct on PDF pages 18 to 20 (62 image labels): read by OCR 32 -> 44, detected (incl. near and partial) 23 -> 31 (37.1% -> 50.0%), Tesseract alone vs both. `python -m cerberus.eval.recall ... --location image --compare-ocr`. Still weak: the terminal screenshot on page 20 (7 px text, masked whole), e-mails inside pictures (3 of 22), and a value whose field label is on the line above it (the badge date of birth is covered by shape but not reported as a finding). The sample badge's photo is a drawn silhouette: no face is detected, correctly, and it stays visible.
- [x] Detection fixes for text already read inside pictures (9 Oct, `detect/rules`): a PAN run into its label ("PAN" glued to the value); initials plus surname as a PERSON finding inside pictures (`initials_and_surname`, not when the surname is an ordinary word of the document); an e-mail whose "@" was read as a letter, found by ending in a domain the documents use (`email_at_misread`); a staff ID after a misread "="; a card number already masked in the source (`card_already_masked`, sub-type masked); and a short digit-free line directly above a value inside a picture counts as its label (the badge date of birth). The document's own vocabulary (`allowlist.common_words`) now lives in detect. The picture fallback repeats until stable (up to 3 rounds). The recall tool has a fourth outcome, **garbled** (found in the right place, more than two characters wrong, at least 80% alike), reported in its own column and never folded into strict, near or partial. Measured on PDF pages 15 to 21 (142 labels): overall 78.2% -> 82.4% with near and partial, 89.4% with garbled; **images 50.0% -> 59.7%, 75.8% with garbled**; 15 of 62 picture labels still missed, 11 of them never read by OCR. Full run on the three files with these changes (`outputs/run5`): both verification checks passed; 1,153 findings, 330 identities; pictures word by word / whole: PDF 9 / 4 of 13, DOCX 24 / 21 of 45, PPTX 9 / 0 of 9; 25 of 67 in the review queue; page 18 columns still fully covered (checked by eye); about 14 minutes with cached readings.
- [ ] RapidOCR as the second OCR engine: **on branch `ocr-rapidocr` only, not merged** (9 Oct). `cerberus/extract/rapid.py`; `images.second_engine()` picks RapidOCR if installed, else docTR; `CERBERUS_OCR=tesseract|doctr|rapidocr` chooses. It runs the PP-OCR small detection and recognition models shipped inside the `rapidocr` package (v6 in release 3.10) on onnxruntime, reads the picture as it is with no enlarging or tiling, and gives word positions. Measured end to end on PDF pages 15 to 21 (142 labels), Tesseract alone / with docTR / with RapidOCR: read by OCR 78.9% / 87.3% / 93.7%; detected with near and partial 73.2% / 82.4% / 88.0%; with garbled 75.4% / 89.4% / 90.1%. **Inside pictures: 59.7% -> 72.6% detected (exact 33.9% -> 50.0%)**; names 60 of 60. Five labels found only with RapidOCR, four only with docTR (some e-mails), so all three together would read slightly more. Extraction of the PDF: about 126 s uncached against 255 s or more with docTR. Full run on the three files (`outputs/run6`): both verification checks passed; 1,159 findings, 321 identities; pictures word by word / whole: PDF 9 / 4 of 13 (pages 9, 13 and both on 20), DOCX 26 / 19 of 45, PPTX 9 / 0; 23 of 67 in the review queue (was 25). The residue check was refined on the branch: only a sideways cut on a mask's own line counts, and not the clipped end of an ordinary small-letter word. Open before merging: only page 18 was looked at by eye (table columns covered; more of the attestation paragraph is covered than with docTR; the tail of an e-mail domain shows at the stamp's edge); the DOCX pictures and pages 19 and 28 have not been checked by eye; the agreement threshold was not re-derived for this engine; `rapidocr` installs `opencv-python-headless` beside `opencv-python`. One unmatched finding on page 18 looks like a real value the labels miss (an SSN in the attestation paragraph).
- [ ] After the submission, when the repository is public (decided 9 Oct, deliberately not before): learned super-resolution before OCR (slow on a CPU, and it can invent characters, which is worse than an unread one for an identifier); a small local vision-language model reading pictures (minutes per picture on a CPU, and a generative model's output cannot be checked against source text inside a picture); other engines such as PaddleOCR proper (its install would downgrade numpy and replace OpenCV here), EasyOCR, Surya, TrOCR; fine-tuning a recogniser on small on-screen print.
- [x] PDF tables: pixel evidence added. A dark header band plus ruled lines marks out the table and its rows; single-row strips are merged and bridged columns split. On the sample, 59 of 67 detected tables have a clean header row.
- [ ] PDF tables, remaining: tables continuing across a page break (no header band on the second page), two adjacent prose columns merged, cell text on coloured badges not read by OCR.
- [x] OCR for pictures in PPTX, DOCX and typed PDF pages (`images.py`): read once per distinct picture, EMF/WMF/SVG converted through LibreOffice, enlargement chosen from measured text height, lines emitted as image content. Unopenable or low-confidence pictures are quarantined. Extraction now covers every channel in all three formats.
- [x] Structure-retention metric (`cerberus/eval/structure.py`): scores extraction against hand-corrected reference files in `labels/structure/` (a small Markdown dialect, one element per line). Export a page, correct it by eye, delete the word UNCHECKED, score. Reports the retained share, the reasons for losses, and character accuracy; text inside pictures is scored separately.
- [x] Whole-file structure score for DOCX and PPTX (`cerberus/eval/native.py`, `python -m cerberus.eval.native FILE`): the reference is read from the file itself with python-docx / python-pptx and raw XML, sharing no code with the extractors. On the sample (9 Oct, evening): **PPTX 364 of 373 elements = 97.6%** (order not scored: a slide has no reading order of its own; the 9 lost are slides with no title placeholder, where the extractor promotes the largest text to a heading and the reference calls it a paragraph); **DOCX 1,318 of 1,318 = 100%** (order scored). The first DOCX figure was 95.5% with 57 wrong parents. Of those, 40 were the reference being too simple (a heading given an outline level directly, not by a "Heading N" style; cell text that python-docx's `.text` does not read) and 17 were a real extraction gap: the lower part of a vertically merged cell was dropped, so its row lost its first cell. The extractor now keeps it as an empty stand-in block (`native_ref.merged_from`) that reads as the cell it continues. A 100% score from a reference corrected by the same hand that wrote the extractor deserves an independent look. Left out of the reference: DOCX text boxes, headers, footers, footnotes, nested tables.
- [ ] Structure, remaining: have the review session check `eval/native.py` independently; the nine PPTX title cases are a definition question, not a defect.
- [ ] Structure references for the scanned PDF: exported 9 Oct to `labels/structure/` for pages 1, 16, 18, 22, 28, 30 and 35, all still marked UNCHECKED. A person must correct each against the original and delete the word UNCHECKED; until then they are not scored. Page 25 exists, checked by Claude (95.2% retained, 99.9% character accuracy), which is not an independent reference. Only page 16 is pure prose; pages 1 and 35 are the next most prose-heavy.
- [x] Rule detector (`cerberus/detect/rules`): e-mail, phone, staff ID, SSN (full and last-four), tax ID, card (full and last-four), PAN, PESEL (full and already-masked), passport, date of birth. Validators: Luhn, PESEL checksum and date, PAN holder letter, SSN structure with the 900 range accepted and flagged. Staff-ID prefixes are learned from column headers and from codes that follow a name, not hardcoded. `Finding` carries the transparent tags; location is body / table / prose / image.
- [x] NER for names and addresses (`cerberus/detect/ner.py`): GLiNER, model `knowledgator/gliner-pii-small-v1.0` in the gitignored `models/` folder, loaded offline. Table rows and file properties are given their labels as context. Threshold 0.2 (low on purpose), then the shape and word filters in `allowlist.py` restore precision. "organization" and "job title" are offered to the model as distractor labels and discarded.
- [x] Dictionary sweep (`cerberus/detect/dictionary.py`): names found anywhere are searched for in every file, in their usual written forms (without middle initial, abbreviated, reversed), approximately in OCR text. Surnames and first names alone are not swept. Names from pictures and from OCR headers/footers do not seed it. `cerberus.detect.detect()` runs rules, NER and the sweep on the original text and settles overlaps once.
- [x] False-positive gates: a name seeds the sweep only with independent evidence (an e-mail carrying its surname, a staff ID in the same block or row, a person-type column header, an author file property, or two sightings); names on low-confidence OCR words cannot seed. Addresses must contain a street line or a postcode. Optional review file (`detect(..., review=path)`): written on first run with the pipeline's keep/drop decision and evidence per name; a person's edits are applied on the next run. A name without evidence is still searched for exactly as written; only its abbreviated and approximate forms are held back (changed 7 Oct after a detected name was found left in the redacted DOCX text). Review file states: keep, exact, and a person's drop.
- [ ] Detection, remaining: allowlist for organisation contact details (a switchboard number, a letterhead address), single-name mentions (a first name alone), names in all capitals with a middle initial, and calibration of thresholds once labels exist.
- [ ] Set-up note for a new machine: install CPU torch and torchvision, `pip install -r requirements.txt`, then once: the GLiNER model into `models/gliner-pii-small-v1.0` (about 320 MB), `python -m cerberus.extract.neural --download` (docTR weights into `models/doctr`), and `face_detection_yunet_2023mar.onnx` from the OpenCV model zoo into `models/opencv`. Nothing is fetched at run time; without docTR every picture is masked whole.
- [x] Entity resolution and stable tokens (`cerberus/redact/tokens.py`): values are compared after normalising how they are written; people by full name, with written forms resolving to one person, a surname alone never linking, and an abbreviation linked only if exactly one person fits. A saved vault (`outputs/vault.json`, holds original values) keeps numbering stable across runs. `image_only()` lists identities seen only inside pictures (E.6 #3). On the sample: 1,104 findings resolve to 300 identities, 100 of them people; 2 people appear in more than one file; 41 identities appear only in pictures.
- [x] Redacted text for the LLM (`cerberus/redact/text.py`): tokens replace PII in place, structure kept; text from pictures, quarantined content, file properties and running headers/footers are withheld. On the sample, no detected value remains in any of the three redacted texts.
- [x] Masked PDF (`cerberus/redact/pdf.py`): redactions are applied, erasing the scanned pixels under each finding; tokens are written in the cleared space; every figure and any quarantined page is masked whole; info dictionary and XMP cleared; saved with `garbage=4`, not incrementally. On the sample: 35 of 35 pages, same page sizes, no metadata, 13 figures masked.
- [x] Masked copies of DOCX and PPTX (`cerberus/redact/docx.py`, `pptx.py`, shared `office.py`): tokens are written into the run that held the value, so formatting and layout are kept; every picture is replaced by a grey placeholder and the original image is dropped from the file; the old-Word copy of each text box is kept and overwritten with a copy of the masked current one (removed only if it cannot be paired or holds WordArt text); SVG twins, tracked deletions and revision/comment author names are removed; person-type core properties hold a token or are emptied; flagged link targets are neutralised; DOCX docVars and PPTX alt text are scrubbed. The writer rebuilds each paragraph's text and refuses to write if it differs from what was scanned. On the sample: no detected value remains in any XML part of either masked file, structure counts are unchanged apart from picture text, and both files open (9 slides, 84 pages).
- [ ] Masked DOCX/PPTX, remaining: custom XML parts and application properties are rewritten only when a finding is in them; picture fills on shapes, charts and SmartArt are not handled; a vector picture (EMF, SVG) that is kept is stored as a PNG.
- [x] Verification pass (`cerberus/redact/verify.py`) and one-command pipeline (`cerberus/pipeline.py`, `python -m cerberus.pipeline <folder> --out <folder>`). Outputs are written to `pending`, checked, and moved to `redacted/` and `masked/` only if clean; otherwise everything moves to `withheld/` and the run fails. Check 1 re-extracts each masked file and re-runs the detectors. Check 2 searches every part of each output (as stored and with markup stripped) for every replaced value of 6+ characters. Problems are reported by token and location, never by value. On the sample: both checks pass on all six outputs; a full run takes about 13 minutes.
- [x] Detection repeats until stable (`pipeline.look_again`): after tokenising in memory the detectors run again on the tokenised blocks, and anything new is mapped back to the original text (up to 3 rounds). Added because the model found two names in a multi-name table cell only once their neighbours were tokens. A person span is split at a line break, and the sweep does not match across one.
- [ ] Verification, known narrowing: the model re-scan of the redacted *text* reads it in block form, not the Markdown layout, because the model gave unstable (and in the one case seen, wrong) answers on the re-laid-out text. Rules and the raw search still run on the text file itself. In block form the model does not flag that phrase, so it is left unmasked, which is correct.
- [x] Audit log and summaries (`cerberus/report/audit.py`), written to `<out>/report/` by the pipeline: `audit.csv` (one row per finding with its tags; token, never the value), a summary per file (pages read by OCR, what was withheld, counts by category, location and page) and `exposure.md` across files (identities in more than one file, identities only in pictures, verification result). The report is itself searched for replaced values and deleted if one is found; it is also written, marked FAILED, when a run is withheld. On the sample: 1,115 instances, 304 identities, 42 seen only in pictures.
- [x] PII evaluation tool (`cerberus/eval/pii.py`): writes one CSV per document to `labels/pii/` with a row per finding; a person marks each `ok` or `wrong` and adds `missed` rows; `score` reports precision, recall and F1 overall, by entity and by location. Re-exporting keeps verdicts already given.
- [x] Recall against independent ground truth (`cerberus/eval/ground_truth.py`, `recall.py`): loads a tab of the label workbook in `labels/`, pairs labels with findings by page and normalised text (counted; exact, near = within 2 edits, partial = one contains the other) and reports recall by type, location and page, extraction coverage, misses with context and unmatched findings. Row-level files go to `outputs/eval/`; only counts are printed. `python -m cerberus.eval.recall <document> --labels <workbook> --sheet <tab> --pages 15-21`.
- [ ] First measurement (8 Oct, PDF pages 15 to 21, 142 labels, PDF processed alone, about 4.5 minutes): strict recall 60.6%, 72.5% with near and partial. Tables, prose and body: 100% with near and partial. Images: 37.1%. All 39 misses are in pictures, 35 of them never read by OCR; 10 unmatched findings. Figure OCR is the bottleneck, not detection. Pictures are masked whole, so these misses do not reach downstream.
- [ ] PII labelling by the team: mark the three sheets in `labels/pii/` (927, 124 and 53 rows) and add the misses by reading each document. Nothing is marked yet, so there is no precision or recall figure.
- [x] Demo notebook (`notebooks/demo.ipynb`, helpers in `cerberus/demo.py`). Part 1 runs the whole pipeline live on three made-up files written by `cerberus/eval/synthetic.py` (a scanned PDF page, a DOCX with a table and an access-card picture, a PPTX with out-of-order boxes and abbreviated names): 43 findings, 16 identities, verification passed, about 35 seconds. Part 2 reads a finished run on the case files from `outputs/run3` (counts, reports, redacted text, a page before and after). Part 3 prints the recall tables from `outputs/eval`. Only part 1 shows original values, and those are invented. The notebook is committed without outputs (nbstripout). Start it with `python -m notebook notebooks/demo.ipynb` from the repository root.
- [x] Two bugs found by running the notebook (9 Oct): the name model's folder was looked up relative to the current folder, so nothing ran from `notebooks/`; and a greyscale scanned page came out garbled in the masked PDF, because the library's bulk image recompression mishandles losslessly stored greyscale images. Redacted page images are now repacked one by one as JPEG (`redact/pdf._repack_images`). The case PDF (colour JPEG pages) was not affected; re-masked with the new code it is the same size (about 12 MB) and looks the same, but the full pipeline has not been re-run on it since this change.
- [x] Review of 9 Oct (`outputs/review/review_2026-10-09.md`, by another Claude session; gitignored), acted on the same day: cell-level picture masking, residue check and review queue (B3); whole-file structure score (B2); upload, fast mode and timings (S5); names read only into a running header or footer are dropped unless the same name is found in the document's own text (S4, `allowlist.without_furniture_names`); unused image relationships are dropped, which removes the SVG twin left in the masked DOCX (S7); `requirements.lock`, Pillow, lxml and opencv-python listed, README with set-up and run steps (S6). Run on the three files (9 Oct, `outputs/run4`): both verification checks passed; 1,137 findings, 315 identities; pictures word by word / whole: PDF 9 / 4 of 13 (pages 9, 19 and both on 20), DOCX 24 / 21 of 45, PPTX 9 / 0 of 9; **25 of 67 pictures are in the review queue** (21 low agreement, 2 PII still readable on the re-scan, 1 scrap beside a mask, 1 low confidence). Page 18: risk-owner and reviewer-note columns fully covered in all seven rows, checked by eye.
- [ ] From the review, not done: B1 labelling (the other four workbook tabs, PPTX and DOCX labels, verdicts in `labels/pii`, "checked, no PII" rows) is human work; S2 recording which candidates merge or the allowlist dropped; S3 the mobile number in the page 18 reviewer-note column is covered but not reported as a finding (the "tax identifier" in that figure's caption is the PAN); S8 three leftover spots in the masked PDF to look at by eye (a phone-shaped number on page 4, an initial-plus-word on page 14, a digit run on page 35); N5 timestamps and the title left in file properties; N6 no test for a hyphenated surname variant. The client name stays in this file until the rewrite §2.5 requires before the repository goes public (decided 9 Oct).
- [x] Demo, after review (9 Oct): an upload button in the notebook (`ipywidgets`; files are copied into the samples folder and nowhere else), a documented fast mode (`FAST = True`, one OCR engine, every picture masked whole and queued), and a three-page made-up scan (prose, a ruled table, a framed access-card figure). Timed on this laptop, whole pipeline with verification: made-up PPTX plus the three-page scan 46 s normal, 27 s fast; all three made-up files 50 s. Part 2 of the notebook is skipped until its run folder has a finished report.
- [ ] Demo, remaining: a rehearsal on the laptop that will present (Tesseract, LibreOffice, CPU torch and torchvision, the three model folders, and `outputs/run5` and `outputs/eval` copied across); decide which pages to show in part 2.
- [ ] Deck (built separately, not in this repo).

## 10. Output runs on this machine (for a reviewer)

Everything below is under `outputs/`, which is gitignored and holds real case content. Read it locally only; quote nothing from it in committed files or `notes/` (§2). `vault.json` in each folder maps tokens to real values: do not open it for a review.

Each run folder has the same layout: `redacted/` (tokenised text for the LLM, one `.md` per file), `masked/` (the PDF, DOCX and PPTX with PII removed), `report/` (`audit.csv`, a `.summary.md` per file, `exposure.md`), and from run2 on, `cache/` (OCR readings of pictures).

| Folder | Date | Code that produced it | Pictures | State |
|---|---|---|---|---|
| `outputs/run6/` | 9 Oct, night | Branch `ocr-rapidocr` (RapidOCR as second engine) | Tesseract + RapidOCR. Word by word / whole: PDF 9 / 4, DOCX 26 / 19, PPTX 9 / 0 | Verified. The latest outputs; becomes current once the branch is merged. Its `cache/` makes the final run quick. |
| `outputs/run5/` | 9 Oct, evening | The commit after `01bc5e1` (detection fixes inside pictures, merged-cell stand-ins, repeated fallback) | Two OCR engines | **Current.** Full run on the three files with these changes (`outputs/run5`): both verification checks passed; 1,153 findings, 330 identities; pictures word by word / whole: PDF 9 / 4 of 13, DOCX 24 / 21 of 45, PPTX 9 / 0 of 9; 25 of 67 in the review queue; page 18 columns still fully covered (checked by eye); about 14 minutes with cached readings. |
| `outputs/run4/` | 9 Oct, afternoon | The commit after `b85a843` (cell-level picture masking, residue check, review queue, footer rule) | Two OCR engines. Run on the three files (9 Oct, `outputs/run4`): both verification checks passed; 1,137 findings, 315 identities; pictures word by word / whole: PDF 9 / 4 of 13 (pages 9, 19 and both on 20), DOCX 24 / 21 of 45, PPTX 9 / 0 of 9; **25 of 67 pictures are in the review queue** (21 low agreement, 2 PII still readable on the re-scan, 1 scrap beside a mask, 1 low confidence). Page 18: risk-owner and reviewer-note columns fully covered in all seven rows, checked by eye. | Superseded by run5, and since 14:38 on 9 Oct **mixed**: a later run was withheld here (`withheld/` holds its outputs) and rewrote most of `report/`. Its `masked/`, `redacted/` and `crops/page18_figure8_masked.png` are still the verified outputs of `01bc5e1`. |
| `outputs/run3/` | 9 Oct, 10:12 | `12dc1df` (narrowed covering rules, per-picture fallback) | Two OCR engines. Word by word / whole: PDF 8 / 5 of 13, DOCX 24 / 21 of 45, PPTX 9 / 0 of 9 | Superseded. The run the review of 9 Oct looked at: on page 18 a name was left visible in the risk-owner column. 1,139 findings, 316 identities. |
| `outputs/run/` | 8 Oct, 22:20 | `2a78bc3` | Tesseract only. Every picture masked whole | Complete and verified. The baseline to compare run3 against. 1,115 findings, 304 identities. |
| `outputs/run2/` | 9 Oct, mixed | `9aa0239`, then a later run that could not finish releasing | Mixed | **Do not review.** A run on 9 Oct passed verification but could not replace the masked PDF, which was open in a viewer. `redacted/` and the masked PPTX are from that run; the masked PDF and DOCX and `report/` are from the first two-engine run (00:26), which over-masked pictures; `pending/` holds the PDF and DOCX that were not moved. |
| `outputs/masked/`, `outputs/redacted/`, `outputs/vault.json` | 7 Oct | Before the verification pass | All whole | Superseded. Ignore. |
| `outputs/preview/` | 6 Oct | Rule detector only | n/a | Stale HTML preview showing original values. Ignore. |
| `outputs/review/names.txt` | 7 Oct | Name review file | n/a | Optional input, not an output. Unedited. |

Evaluation outputs (row-level files hold real values):

| Folder | What it is |
|---|---|
| `outputs/eval/` (top level) | Recall on PDF pages 15 to 21, all 142 labels, Tesseract only, 8 Oct. `summary.txt` has the aggregate tables. |
| `outputs/eval/opt1/` | Recall on PDF pages 15 to 21 after the detection fixes of 9 Oct, with the garbled outcome. The current figures. |
| `outputs/eval/ocr/` | Recall on image labels only, PDF pages 18 to 20 (62 labels): `tesseract/` and `both/` hold the two runs, `ocr_comparison.txt` sets them side by side. Run on 8 Oct, before the covering rules were narrowed; narrowing changed masking, not detection, so the figures still stand. |

Ground truth is `labels/Cerberus_PII_Labels.xlsx`, tab "Aryaman" (PDF pages 15 to 21, 142 rows, one labeller). The other tabs and `labels/pii/*.csv` are not filled in.

Where to look first in run5, and what is already known to be imperfect:

- Masked PDF page 18 (form: the risk-owner and reviewer-note columns should be fully covered, with risk references, descriptions and ratings readable), page 28 (badge), and the pictures in `report/review_queue.csv`, which are masked whole.
- Known over-masking: on page 18 one of seven reference codes, one control rating that shares a rectangle with the name beside it, and parts of the attestation paragraph; short button labels in DOCX screenshots. A capitalised word the document never writes in small letters is treated as a possible surname, so a product name inside a picture is covered.
- Known under-masking risk: text neither OCR engine saw as a word stays visible in a word-by-word picture. Checked by eye on PDF pages 15, 18, 19, 28 and two DOCX screenshots only.
- Not masked on purpose: the badge photo on page 28 (a drawn silhouette, not a face).
- No precision figure exists: nobody has marked false positives outside pictures.

To reproduce run5 (about 20 to 30 minutes with its cache, about an hour without): `python -m cerberus.pipeline data/case2 --out outputs/run5`. Close any open output file first. `--pictures whole` gives the baseline behaviour with the current code.

## 11. Freeze and runbook (written 9 Oct 2026; code frozen from here)

**State.** `main` = two engines Tesseract + docTR, verified (`outputs/run5`). Branch `ocr-rapidocr` = Tesseract + RapidOCR, verified (`outputs/run6`), better recall, to be merged by the owner. No new features before 14 October: only the final run, the deck and rehearsal. A new session should read §5 (design), §9 (what exists, with numbers), §10 (which output folder is which) and this section. The reasoning behind each decision, written for a beginner with likely judge questions, is in `notes/extraction-design.md` (gitignored: give that file to a chat that cannot see the folder).

**Merge the branch** (owner, once):

```
git checkout main
git merge ocr-rapidocr
git push
```

**Final verification run** (about 15 minutes with the cache, 25 to 30 without). The output folder is created by the pipeline; nothing has to be made by hand. Close any output file open in Excel, Word or a PDF viewer first.

```
xcopy outputs\run6\cache outputs\run7\cache /E /I /Q
.venv\Scripts\python.exe -W ignore -m cerberus.pipeline data\case2 --out outputs\run7
```

The first line is optional and only saves time. Success ends with `verification: both checks passed on every output` and leaves `outputs\run7\masked`, `redacted` and `report`. If it ends with `FAILED VERIFICATION`, nothing was released (the outputs are in `outputs\run7\withheld`): do not present that folder; use `outputs/run6`.

**Checks after the run** (two minutes, by eye): open the masked PDF at pages 18 and 28 (no name, initial, number or e-mail readable in the figures); open `report\exposure.md` (verification passed; review-queue count); open `report\review_queue.csv` and close it again.

**Recall figures for the deck** (optional, about 12 minutes, counts only on screen):

```
.venv\Scripts\python.exe -W ignore -m cerberus.eval.recall data\case2\Cadence_Group_Risk_Management_Policy__v6.0.pdf --labels labels\Cerberus_PII_Labels.xlsx --sheet Aryaman --pages 15-21 --out outputs\eval\final
```

**Structure figures for the deck** (one minute): `.venv\Scripts\python.exe -W ignore -m cerberus.eval.native data\case2\Cadence_TPRM_Training.docx data\case2\Cadence_Financial_Group_Organizational_Pack.pptx`

**The demo itself.** `.venv\Scripts\python.exe -m notebook notebooks\demo.ipynb`, then Run > Run All Cells. Part 1 runs the whole pipeline live on three made-up files (about a minute; `FAST = True` halves it and masks every picture whole). Part 2 reads the finished run from `outputs/run7` (the notebook's `CASE_OUT`); if that folder has no report it says so and skips. Part 3 prints the recall tables from `outputs/eval`. To process a file a judge hands over: use the upload button in part 1, set `FAST = True` if it is long. Do not save the notebook after part 2 has shown real pages. Rehearse once on the presenting laptop: it needs Tesseract, LibreOffice, the `.venv`, the `models/` folder, `data/case2` and `outputs/run7`.

**Numbers to quote, and their limits.** Recall is measured on PDF pages 15 to 21 only (142 labels, one labeller): typed text, tables and prose 100% with near and partial matches; inside pictures 59.7% on `main`, 72.6% on the branch; anything not read reliably is masked whole and listed in the review queue (25 of 67 pictures on `main`, 23 on the branch). Structure retained: DOCX 1,318 of 1,318, PPTX 364 of 373, whole file; scanned PDF only page 25 (95.2%), checked by Claude. There is no precision figure: nobody has marked false positives. After the final run, quote that run's own numbers.

**Deliverables left.** (1) The deck (.pptx, 10 to 15 minutes): blueprint diagram, architecture, extraction and detection logic, outcomes, demo; build it after the final run so its numbers and screenshots match; never show real values, only tokens. (2) Team labelling, if time: other workbook tabs, verdicts in `labels/pii`, the seven UNCHECKED pages in `labels/structure`. (3) Rehearsal on the presenting laptop. (4) A person looks at the review queue and at masked PDF pages 4, 14 and 35.

## 12. Final measurement and deck (10 Oct 2026)

`main` = `fbb96a4` (RapidOCR merged). The verified run to present is `outputs/run9` (both checks passed; 1,159 findings, 321 identities; 23 of 67 pictures in the review queue). The notebook reads `outputs/run9` for part 2 and `outputs/eval/final` for part 3 (updated 10 Oct; executed end to end without error).

**All five labellers' ground truth, scored 10 Oct** (1,094 labels: the whole PDF, all nine slides, parts of the DOCX; row-level results in `outputs/eval/final/`). "Found" = exact, within two characters, or overlapping.

| File | Labels | Found | Notes |
|---|---|---|---|
| PPTX | 122 | 100% (122 exact) | 122 findings, all match a label |
| PDF | 919 | 95.3% (strict 84.2%) | tables 98.3%, prose 97.0%, body 96.4%, pictures 77.7%; 882 of 973 findings match a label (90.6%) |
| DOCX | 53 | 90.6% | tables 100%, pictures 80%; Word pages 43 to 63 were not labelled, so no precision figure |
| All | 1,094 | 95.6% (96.2% with garbled) | text 98.1% of 957, pictures 78.1% of 137 |

Limits to state: the 90.6% is a floor on precision, not a measurement (the 91 unmatched PDF findings are false positives or values the labellers missed; nobody has gone through them). Of the 16 text labels not found, 2 are real misses that reach the redacted text (a first name used alone on page 22; a partial bank account number on page 23, for which there is no rule), 9 are phone numbers on pages 13 and 35 not present in the extracted text as labelled (not investigated), and 5 are artefacts of parsing one labeller's list. One labeller's sheet arrived as a PDF listing with no type column (type shown as UNSPECIFIED).

**Deck:** `outputs/deck/Cerberus_CaseStudy2.pptx` (12 slides, speaker notes on each; gitignored). It shows no real values; the before/after picture is the made-up scan. Built from the numbers above.

