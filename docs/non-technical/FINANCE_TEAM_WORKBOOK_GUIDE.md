# Finance Team Workbook Guide

## Workbook to use

Open **`team_recon_submission__<pair_id>.xlsx`** (not the internal audit workbook unless you need full traceability).

## Visible sheets

| Sheet | Purpose |
|-------|---------|
| **Team_Guide** | Instructions and limitations |
| **Working** (org + party) | Standardized ledger with match status |
| **Summary** | Reconciliation totals and status |
| **Annex_*** | Transactions grouped by type |
| **Rows_Needing_Improvement** | Items requiring your review |

Internal audit sheets (Match Evidence, AI Decision Audit, Validation Report) are **hidden** but preserved for auditors.

## How to review

1. Start with **Summary** — check overall status and closing balance difference.
2. Open **Rows_Needing_Improvement** — resolve HIGH priority first.
3. Use annexures to drill into Invoice, Payment, Credit Note, etc.
4. Cross-check against source PDFs using reference, date, and amount on each row.

## Excel formulas

- Working ledger **net** columns and Summary totals use formulas tied to Excel Tables.
- You may **paste new rows directly below** a Working table; tables expand and formulas fill down where supported.
- Formulas **do not** perform reconciliation — they only calculate display totals from engine output.

## Manual decisions

Use these columns (left blank by the engine):

- `reviewer_comment` — your notes
- `manual_status` — e.g. Accepted, Rejected, Deferred
- `manual_issue_code` — optional override of issue category
- `selected_match_group_id` / `reviewer_selected_*_row_ids` — document manual pairing
- `reviewed_by`, `reviewed_at`, `override_reason` — audit trail

**Do not** move rows between sheets to “fix” matches. Record decisions in these fields.

## When to rerun the engine

Rerun after:

- Adding pasted source rows below Working tables
- Changing formalized source data
- Applying manual resolutions you want reflected in match groups

Authoritative matching, annexures, and review rows are regenerated only by rerunning the pipeline.

## What not to edit

- Hidden audit sheets (unless instructed by audit)
- Engine-owned columns: `match_group_id`, `decision_source`, `match_confidence`, raw evidence columns
- Do not delete Review Queue rows — mark via manual fields instead
