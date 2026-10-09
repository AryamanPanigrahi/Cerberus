# Cerberus

A local pipeline that reads PDF, DOCX and PPTX files (scanned pages and pictures included), finds and classifies personal data, replaces it with stable tokens, and checks its own output before releasing anything. No document content leaves the machine.

```
extract -> detect (rules, a local language model, a name sweep) -> resolve to tokens -> redact -> verify -> release
```

## What it produces

For a folder of files, in the output folder:

| Folder | Contents |
|---|---|
| `redacted/` | The text of each file with tokens in place of personal data: what a downstream LLM may be given |
| `masked/` | Each file in its original format, same pages and layout, with the data removed |
| `report/` | `audit.csv` (one row per finding: where, what kind, which rule, how confident), a summary per file, `exposure.md` across files, and `review_queue.csv` (pictures a person must look at) |
| `vault.json` | Which token stands for which value. Stays on the machine. |

Outputs are released only if a second pass over them finds no personal data left. Otherwise nothing is released and the run fails.

## Set-up

Tested on Windows with Python 3.13.

1. Programs, installed separately:
   - [Tesseract OCR](https://github.com/tesseract-ocr/tesseract). If it is not on the PATH, set `TESSERACT_CMD` to the executable.
   - [LibreOffice](https://www.libreoffice.org/), used to convert EMF, WMF and SVG pictures. If it is not found, set `SOFFICE_CMD`.

2. Python packages, in a virtual environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

   `requirements.lock` records the exact versions this was last run with (`pip install -r requirements.lock` reproduces them). For tests and the notebook, also `pip install -r requirements-dev.txt`.

3. Models, downloaded once into the `models/` folder (not in the repository). Nothing is fetched while the pipeline runs.

   | Model | Where it goes | How |
   |---|---|---|
   | Name detection (GLiNER, about 320 MB) | `models/gliner-pii-small-v1.0` | Download `knowledgator/gliner-pii-small-v1.0` from Hugging Face into that folder |
   | Second OCR engine (docTR) | `models/doctr` | `.venv\Scripts\python -m cerberus.extract.neural --download` |
   | Face detection (OpenCV YuNet) | `models/opencv/face_detection_yunet_2023mar.onnx` | From the OpenCV model zoo |

   Without the docTR weights the pipeline still runs; every picture is then masked whole.

## Run

```bash
.venv\Scripts\python -m cerberus.pipeline <folder of files> --out <output folder>
```

| Option | Effect |
|---|---|
| `--pictures whole` | Mask every picture whole, instead of word by word where two OCR engines agree |
| `--no-ner` | Rules only: fast, but finds no names |
| `--review` | Write, or apply, a file in which a person can confirm or reject detected names |

Fast mode: set the environment variable `CERBERUS_OCR=tesseract` to read pictures with one OCR engine. It is several times quicker on files with many pictures; every picture is then masked whole and listed in the review queue. Text and tables are handled the same.

Try it on made-up files (about a minute):

```bash
.venv\Scripts\python -m cerberus.eval.synthetic samples
.venv\Scripts\python -m cerberus.pipeline samples --out samples_out
```

## Demonstration notebook

```bash
.venv\Scripts\python -m notebook notebooks\demo.ipynb
```

It generates the made-up files (or takes an upload), runs the pipeline on them live, and shows the findings, the redacted text, the masked files and the report.

## Tests

```bash
.venv\Scripts\python -m pytest
```

## Measuring it

| Question | Command |
|---|---|
| How much personal data was found, against labels a person made? | `python -m cerberus.eval.recall <file> --labels <workbook.xlsx> --sheet <tab>` |
| How much of a DOCX or PPTX's structure survives extraction (whole file)? | `python -m cerberus.eval.native <file>` |
| The same for scanned pages, against pages a person corrected | `python -m cerberus.eval.structure export`, then `score` |
