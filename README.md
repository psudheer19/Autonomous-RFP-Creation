# Autonomous-RFP-Creation
Extracts formal RFP-style requirements from telecom Feature Description/Requirements Documents (FDDs/FRDs) using the Claude API, and outputs them as a structured CSV.

## What's in here

- `analyzer_optimized.py` — main script. Reads a folder of documents, calls Claude once per requirement category, and writes `requirements_output.csv`.
- `prompts.py` — registry of prompt profiles (`smsc`, `mmsc`, `smppgw`, `tpd`). Each profile defines the system prompt and requirement-ID prefix for a product line.

## Setup

```bash
pip install anthropic pypdf python-docx
export ANTHROPIC_API_KEY="your-key-here"
```

`pypdf` is only needed for `.pdf` inputs, `python-docx` only for `.docx`/`.doc` inputs.

## Generating a CSV from a folder of documents

1. Put your source documents (PDF, DOCX, TXT, MD, etc.) in a folder, e.g. `MMSC/`.
2. Run:

```bash
python3 analyzer_optimized.py MMSC --prompt mmsc
```

- The folder argument can be a relative path (resolved next to the script) or an absolute path.
- If you omit the folder argument, the script looks for a folder named `FDD`, `fdd`, `documents`, `docs`, `input`, or `files` next to the script.
- `--prompt` selects which prompt profile to use (see below). Defaults to `smsc`.
- Output is always written to `requirements_output.csv` in the script's directory, **overwriting any existing file with that name**. Rename or move the output before re-running on a different document set if you want to keep it.

### Available prompt profiles (`prompts.py`)

| Profile | Flag | Requirement ID prefix | Notes |
|---|---|---|---|
| SMSC | `--prompt smsc` (default) | `REQ-SMSC-...` | Original profile, no subcategories. |
| MMSC | `--prompt mmsc` | `REQ-MMSC-...` | Prioritizes Peak Traffic Requirements, then 3GPP TS 22.140/23.140/26.140, OMA-IOP SMIL, VZ LI Spec. Excludes MM5, MM8, and SNMP alarm requirements. Functional Requirements are grouped into MM1/MM3/MM4/MM7/CALEA/Billing/Security/Core subcategories. |
| SMPP Gateway | `--prompt smppgw` | `REQ-SMPPGW-...` | Prioritizes SMPP Gateway Peak Traffic Requirements, then SMPP v3.4 spec, then SMPP Gateway Functions. Subcategories: Session Management, PDU Handling, Routing, Throttling, Error Handling, Security, Billing, Core. |
| TPD | `--prompt tpd` | `REQ-TPD-...` | All source documents treated as equally important (no priority order); instructs Claude to deduplicate requirements found in multiple documents. Subcategories: Delivery, Protocol, Authentication, Error Handling, Security, Billing, Core. |

To add a new product line, add a new profile dict to `prompts.py` and register it in the `PROMPTS` dict at the bottom of the file — no changes to `analyzer_optimized.py` are needed.

### Output format

Each CSV has one section per pillar (in this order): Functional Requirements, Interface & Integration, Performance & Scalability, Operations & Administration. Columns are:

```
Requirement ID, Feature Name, Requirement Statement, Compliance Type
```

Requirement IDs follow `REQ-<prefix>-<category>-<sequence>`, e.g. `REQ-MMSC-FUNC-014`. For profiles with Functional Requirements subcategories, a subcategory header row is inserted before each group.

## Appending a single document to an existing CSV (`append_fr15561.py`)

This script is a template for adding requirements from **one new document** into an **already-generated** CSV without re-running the full analysis or disturbing existing rows/IDs. It was built for adding `FR15561` into the MMSC v0.3 CSV, dropping two obsolete rows in the process — treat it as a reference to copy/adapt for similar one-off merges rather than a generic tool.

It hardcodes:
- The input CSV path and filename
- The input PDF path and filename
- Which row IDs (if any) to drop before renumbering
- The system prompt to use for the new document (copied inline from the `mmsc` profile in `prompts.py`)
- The output filename

To reuse it for a different document/CSV, open the script and edit the `Config` section at the top (`INPUT_CSV`, `INPUT_PDF`, `OUTPUT_CSV`, `ROWS_TO_DROP`), then run:

```bash
python3 append_fr15561.py
```

It will:
1. Load the existing CSV, drop any rows listed in `ROWS_TO_DROP`, and renumber the affected section's IDs consecutively.
2. Extract text from the new PDF and run it through Claude, one call per category pillar.
3. Insert the new requirements at the end of their respective sections, continuing ID numbering from the highest existing number in each section.
4. Write the result to a new output CSV (existing input files are never modified).

## Notes

- Both scripts use prompt caching (`cache_control: ephemeral`) so that the (large) combined document text is only billed at full price on the first of the four category calls per run.
- Both scripts retry automatically (up to 5 times, with backoff) on `429` rate-limit errors from the API.
- Extended-thinking responses may include a `ThinkingBlock` before the actual text — both scripts already handle this by scanning `message.content` for the first block that has a `.text` attribute.               
