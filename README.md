# Finance Ledger Reconciliation Automation

A local, office-internal tool for reconciling accounting ledgers between an organisation
and its trading parties.

## Project purpose

Given pairs of ledgers (an organisation ledger and the matching party ledger), the project
will eventually help reconcile transactions between the two sides. Reconciliation is done
in stages so that every step is auditable and traceable back to the original documents.

## Current scope: full reconciliation workbook generation

The project now supports **final, deterministic reconciliation workbook generation**.
The pipeline runs in disciplined stages so every step is auditable:

1. **Raw PDF context extraction** - reads the source PDFs for a ledger pair and writes
   their raw text, lines, blocks, words, and tables into an Excel workbook for review.
2. **Deterministic formalization** - parses each PDF into a strict, audit-ready, two-ledger
   workbook (canonical schema, opening/closing balances, per-page audit, review queue).
   AI is **optional** here and only ever used in a token-controlled way (see below).
3. **Deterministic reconciliation** - Python matches organisation and party transactions
   (staged exact/containment/fuzzy reference + amount + date + type-mirror rules), builds
   annexures by canonical label, a styled Summary, a Master Match Table, a Review Queue, a
   Validation Report, a Formula Audit, and writes a submission-quality workbook visually
   modeled on the STARTLING ACHIEVEMENT reference workbook.

### Division of responsibility (architecture)

- **Deterministic Python owns** reconciliation, matching, annexure generation, summary
  generation, validation, formula generation, and workbook writing.
- **AI may only assist** with messy source interpretation, and never makes a final match
  decision. The three bounded AI modes are:
  - `layout_only` - profile column layout from a tiny sample,
  - `repair_failed_rows` - repair only the specific failed rows sent to it (dates),
  - `group_unknown_labels` - map a deterministically-unclassified row ("Unknown") to a
    predefined canonical label, or return `UnknownNeedsReview`. It can never invent a new
    canonical label; canonical labels live only in `config/type_labels.json`.
- All AI usage is **input-limited** (character cap + conservative estimated-token budget)
  and **output-limited** (`max_tokens`), so free/limited tiers stay bounded. The default
  `AI_FORMALIZATION_MODE=off` guarantees no model is ever called.

Type labels (Invoice, Payment, etc.) are **standardization labels only**, not reconciliation
decisions. A "Sale" on the organisation side maps to the same `Invoice` label as a
"Purchase" on the party side; the deterministic reconciliation engine decides the actual
correspondence.

## Folder structure

```
recon/
  README.md
  .gitignore
  .env.example
  requirements.txt
  pyproject.toml

  .cursor/
    rules/
      finance_recon_project.mdc          # project guardrails for AI assistants

  data/
    00_original_uploads/                 # untouched copies of every uploaded file
      pdfs/
      workbooks/

    01_source_ledgers/                   # source ledgers grouped by role
      org_ledgers/
        good_luck/
        elegant_crafts_india/
      party_ledgers/
        baby_and_mom__good_luck/
        elegant_crafts_india/

    02_work_pairs/                       # one self-contained workspace per ledger pair
      pair_001_baby_and_mom__good_luck/
        input/
          org_ledger/                    # AccountLedger_GOOD LUCK.pdf
          party_ledger/                  # BABY AND MOM.pdf
        output/                          # raw_pdf_context__<pair_id>.xlsx
        audit/
        notes/
      pair_002_elegant_crafts_india/
        input/
          org_ledger/
          party_ledger/
        output/
        audit/
        notes/

    03_reference_workbooks/
      old_recon_workbooks/               # prior/reference Excel workbooks

    04_outputs/
      raw_context_workbooks/             # central copy of each raw context workbook
      project_context_dumps/             # text dumps of project source (no financial data)

    99_archive/

  config/
    type_labels.json                     # canonical type -> aliases for standardization

  src/
    __init__.py
    config.py                            # settings loaded from .env (incl. AI + token caps)
    extraction/
      extract_pdf_pair_to_workbook.py    # raw PDF -> Excel context workbook
    providers/
      ai_client.py                       # provider-agnostic AI client interface + factory
      openai_compatible_client.py        # OpenAI-compatible client (max_tokens + token budget)
    standardization/
      type_labeler.py                    # raw type -> canonical label (exact/substring/fuzzy)
      ledger_schema.py                   # strict pydantic schema for AI output
      prompt_builder.py                  # builds the strict JSON-only AI prompt
      standardize_pair_with_ai.py        # AI standardization runner -> standardized workbook
    formalization/                       # deterministic PDF -> two-ledger formalized workbook
      formalize_pair_ledgers.py          # orchestrator (extract -> assemble -> optional AI)
      pdf_ledger_extractor.py            # PyMuPDF/pdfplumber layout extraction
      type_classification.py             # confidence-banded canonical label classification
      ai_failed_row_repair.py            # bounded AI date repair for failed rows
      ai_label_grouping.py               # bounded AI grouping of Unknown labels
      ai_layout_profiler.py              # bounded AI layout profiling
    reconciliation/                      # deterministic reconciliation + workbook generation
      build_recon_workbook.py            # single-pair builder (CLI)
      build_all_recon_workbooks.py       # all-pairs runner + run manifest (CLI)
      matching_engine.py                 # staged deterministic org<->party matching
      annexure_builder.py / summary_builder.py / review_queue_builder.py
      unknown_label_builder.py           # deterministic Unknown-label grouping report
      workbook_writer.py / workbook_style.py   # submission workbook + curated styling
      reference_profile.py / reference_style.py # reference discovery + derived theme
      validation.py / formula_builder.py
    excel/
      workbook_io.py                     # shared workbook write helpers
      profile_reference_workbook.py      # structural + formatting profile of a reference workbook
      compare_standardized_to_reference.py # structural similarity check
    tools/
      dump_py_to_txt.py                  # dump project source to a single .txt
      inspect_recon_workbook.py          # read-only integrity audit of a recon workbook

  tests/
    __init__.py
    test_imports.py
    test_config.py
    test_type_labeler.py
    test_schema.py
    test_prompt_builder.py
```

Architecture discipline: source files stay separated, each reconciliation pair lives in its
own workspace under `data/02_work_pairs/`, and raw extraction is kept separate from any
future standardization step.

## Setup on Windows

Create a virtual environment and install dependencies. You do **not** need to change
PowerShell `ExecutionPolicy` if you use the runner scripts below (they call
`.venv\Scripts\python.exe` directly and never run `Activate.ps1`).

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` if you want to customise settings (the defaults work as-is):

```powershell
Copy-Item .env.example .env
```

Do **not** run `Activate.ps1` if PowerShell blocks scripts on your machine.
Do **not** paste API keys into prompts, terminal output, or chat messages.

## Windows-safe AI standardization

Recommended command (PowerShell, no venv activation):

```powershell
.\scripts\run_ai_standardization.ps1
```

Optional pair id:

```powershell
.\scripts\run_ai_standardization.ps1 -PairId pair_001_baby_and_mom__good_luck
```

Fallback command (CMD):

```cmd
scripts\run_ai_standardization.cmd
```

Optional pair id as the first argument:

```cmd
scripts\run_ai_standardization.cmd pair_001_baby_and_mom__good_luck
```

Dry-run (plan chunks only, no AI call):

```powershell
.\.venv\Scripts\python.exe -m src.standardization.standardize_pair_with_ai --pair-id pair_001_baby_and_mom__good_luck --dry-run
```

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Generate final reconciliation workbooks (both pairs)

The single, reliable command path discovers every ledger pair under
`data/02_work_pairs/` (any directory containing an `input/` folder), formalizes and
reconciles each, copies outputs to a central directory, prints exact final paths, and
writes a run manifest:

```powershell
.\.venv\Scripts\python.exe -m src.reconciliation.build_all_recon_workbooks --refresh-formalized
```

Single pair only:

```powershell
.\.venv\Scripts\python.exe -m src.reconciliation.build_recon_workbook --pair-id pair_001_baby_and_mom__good_luck --refresh-formalized
```

Optional bounded AI repair (only with `.env` configured and hosted approval set):

```powershell
.\.venv\Scripts\python.exe -m src.reconciliation.build_recon_workbook --pair-id pair_001_baby_and_mom__good_luck --refresh-formalized --ai-repair-batches 1
```

Flags (forwarded to every pair by the all-pairs runner):
`--refresh-formalized`, `--ai-repair-batches <n>`, `--reference-workbook <path>`,
`--strict` (fail if no reference workbook found), `--dry-run` (plan only, no files).

### Where outputs are written

- Per pair: `data/02_work_pairs/<pair_id>/output/final_recon_submission__<pair_id>.xlsx`
  (and `recon_workbook__<pair_id>.xlsx`).
- Central copies: `data/04_outputs/final_recon_submissions/` and
  `data/04_outputs/reconciliation_workbooks/`.
- Run manifest: `data/04_outputs/run_manifests/recon_run_<timestamp>.json`.

### Final workbook sheets

`README`, `Executive_Summary`, the two raw ledger sheets + their `Working` sheets,
`Summary`, `Master_Match_Table`, one `Annex_<label>` per canonical label, an optional
`Unknown_Needs_Review` annexure (only when unclassified rows exist), `Review_Queue`,
`Validation_Report`, `Formula_Audit`, and `Assumptions_And_Limits`.

### Inspect / validate a generated workbook

```powershell
.\.venv\Scripts\python.exe -m src.tools.inspect_recon_workbook "data/02_work_pairs/pair_001_baby_and_mom__good_luck/output/final_recon_submission__pair_001_baby_and_mom__good_luck.xlsx"
```

This read-only audit reports required sheets, Summary position, annexure count vs
configured labels, formula presence, evidence-column protection, review-queue schema,
and `Validation_Report` FAIL counts. The `Validation_Report` sheet itself uses
`PASS` / `REVIEW` / `FAIL` / `INFO` statuses (REVIEW == warn / needs human attention).

## STARTLING ACHIEVEMENT reference template

The final workbook is visually modeled on
`data/03_reference_workbooks/old_recon_workbooks/STARTLING ACHIEVEMENT - Jan'25 to Jun'25.xlsx`.
Profile it (read-only; the source is never modified) to refresh the captured fonts,
fills, freeze panes, column widths, and page setup used to derive the workbook theme:

```powershell
.\.venv\Scripts\python.exe -m src.excel.profile_reference_workbook --workbook "data/03_reference_workbooks/old_recon_workbooks/STARTLING ACHIEVEMENT - Jan'25 to Jun'25.xlsx"
```

The reconciliation builder reads the resulting profile
(`data/04_outputs/reference_profiles/reference_workbook_profile__startling_achievement.json`)
and applies a curated theme (header/banner palette, page orientation, fit-to-width) on
top of a clean default palette. If the profile is missing, the curated default is used.

## AI usage limits and repair mode

- `AI_FORMALIZATION_MODE=off` (default) means no model is ever called.
- Active modes (`layout_only`, `repair_failed_rows`, `group_unknown_labels`) still require
  the global gate: `AI_ENABLED=true` and, for hosted providers,
  `AI_DATA_APPROVAL=hosted_approved`.
- Repair mode is bounded (`AI_MAX_FAILED_ROWS_PER_REQUEST`), cache-aware
  (`AI_FORMALIZATION_CACHE_ENABLED`), never sends whole ledgers (only the failed rows),
  applies only validated date repairs, preserves all original source evidence, and records
  what was asked and returned.
- Unknown-label grouping sends only minimal, deduplicated context for the distinct
  unclassified raw types and may only choose a predefined canonical label or
  `UnknownNeedsReview`.
- Token limits: `AI_MAX_INPUT_TOKENS_PER_REQUEST` rejects oversized requests before any
  network call (input tokens estimated conservatively), and `AI_MAX_OUTPUT_TOKENS` is sent
  to the provider as `max_tokens`. The API key is stored as a secret and never logged.

## Privacy and financial-data handling

- This is a local, office-internal tool. Real financial documents may live under `data/`.
- All processing of financial documents stays on the local machine. Hosted AI is refused
  unless `AI_DATA_APPROVAL=hosted_approved` is explicitly set, and even then only minimal
  row/sample context is sent - never whole ledgers or source PDFs.
- Source files (PDFs, original uploads, reference workbooks) are never mutated or deleted.
- Do not commit financial PDFs, Excel workbooks, `.env`, or generated outputs anywhere.

## Manual review required before team submission

Before sending a workbook to the finance team, a human must review:

- every row in `Review_Queue` (HIGH priority first), and any `Unknown_Needs_Review` rows;
- the `Validation_Report` (resolve all `FAIL`, assess every `REVIEW`);
- the closing-balance difference and the reconciliation `Status` on the `Summary`;
- candidate (non-strong) matches in `Master_Match_Table`;
- the reviewer-owned columns (`reviewer_comment`, `manual_status`), intentionally left blank.

## Run raw extraction for a ledger pair

```powershell
python -m src.extraction.extract_pdf_pair_to_workbook --pair-id pair_001_baby_and_mom__good_luck
```

Outputs:

- `data/02_work_pairs/pair_001_baby_and_mom__good_luck/output/raw_pdf_context__pair_001_baby_and_mom__good_luck.xlsx`
- `data/04_outputs/raw_context_workbooks/raw_pdf_context__pair_001_baby_and_mom__good_luck.xlsx`

## Commands

1. Improve raw extraction:

```powershell
python -m src.extraction.extract_pdf_pair_to_workbook --pair-id pair_001_baby_and_mom__good_luck
```

2. Profile reference workbook:

```powershell
python -m src.excel.profile_reference_workbook --workbook "data/03_reference_workbooks/old_recon_workbooks/STARTLING ACHIEVEMENT - Jan'25 to Jun'25.xlsx"
```

3. Run AI standardization (only when `.env` is configured and approval is correct):

```powershell
.\scripts\run_ai_standardization.ps1
```

Or manually without venv activation:

```powershell
.\.venv\Scripts\python.exe -m src.standardization.standardize_pair_with_ai --pair-id pair_001_baby_and_mom__good_luck
```

4. Compare standardized workbook to reference:

```powershell
python -m src.excel.compare_standardized_to_reference --standardized "data/02_work_pairs/pair_001_baby_and_mom__good_luck/output/standardized_ledgers__pair_001_baby_and_mom__good_luck.xlsx" --reference "data/03_reference_workbooks/old_recon_workbooks/STARTLING ACHIEVEMENT - Jan'25 to Jun'25.xlsx"
```

5. Dump project context:

```powershell
python -m src.tools.dump_py_to_txt
```

The dump intentionally excludes financial PDFs, Excel workbooks, `.env` secrets, and
generated outputs.

## AI-assisted standardization stage

The standardized workbook is produced by an AI provider and then validated by Python
against a strict schema (`src/standardization/ledger_schema.py`) before being written.

Configuration lives in `.env` (see `.env.example`). Key safety rules:

- `AI_ENABLED=false` by default - the runner fails fast if AI is disabled.
- For `AI_PROVIDER=hosted_openai_compatible`, the runner refuses to make any API call
  unless `AI_DATA_APPROVAL=hosted_approved`. Financial documents never leave the machine
  without this explicit opt-in.
- `AI_API_KEY` is stored as a secret and is never logged or printed.
- `AI_MAX_INPUT_CHARS_PER_REQUEST` caps each AI request (default 12000 for Groq/free
  tiers). Larger raw contexts are split into deterministic chunks automatically.
- `AI_MAX_INPUT_TOKENS_PER_REQUEST` rejects oversized requests before any network call
  (input tokens estimated conservatively), and `AI_MAX_OUTPUT_TOKENS` is passed to the
  provider as `max_tokens` to bound output.

Outputs:

- `data/02_work_pairs/<pair_id>/output/standardized_ledgers__<pair_id>.xlsx`
- `data/04_outputs/standardized_workbooks/standardized_ledgers__<pair_id>.xlsx`

## Final command sequence

```powershell
.\.venv\Scripts\python.exe -m pytest

.\.venv\Scripts\python.exe -m src.excel.profile_reference_workbook --workbook "data/03_reference_workbooks/old_recon_workbooks/STARTLING ACHIEVEMENT - Jan'25 to Jun'25.xlsx"

.\.venv\Scripts\python.exe -m src.reconciliation.build_all_recon_workbooks --refresh-formalized

.\.venv\Scripts\python.exe -m src.tools.inspect_recon_workbook "data/02_work_pairs/pair_001_baby_and_mom__good_luck/output/final_recon_submission__pair_001_baby_and_mom__good_luck.xlsx" "data/02_work_pairs/pair_002_elegant_crafts_india/output/final_recon_submission__pair_002_elegant_crafts_india.xlsx"
```

The default `build_all_recon_workbooks --refresh-formalized` is deterministic (no AI).
Do **not** add `--ai-repair-batches` unless `.env` is configured and `AI_DATA_APPROVAL`
is set correctly.

## WARNING — do not leak financial data

This is a local office project. The repository may contain real financial documents under
`data/`.

**Do NOT commit financial PDFs, Excel workbooks, `.env`, or generated outputs to any public
or remote repository.** Keep this project local. `.gitignore` excludes generated outputs and
secrets, but the original uploads under `data/00_original_uploads/` are intentionally not
ignored so they remain available locally — be careful never to push them anywhere.
