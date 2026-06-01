# CFO Submission Validation Checklist

Complete before sending a reconciliation workbook to the CFO or external accountant.

## 1. Workbook integrity

- [ ] Open `final_recon_submission__<pair_id>.xlsx` in Excel (no corruption).
- [ ] Run inspection tool: `python -m src.tools.inspect_recon_workbook "<path>"`
- [ ] **Validation_Report**: zero `FAIL` rows; assess every `REVIEW`.
- [ ] **Formula_Audit**: zero failures.

## 2. Reconciliation quality

- [ ] **Summary** status and closing balance difference acceptable or explained.
- [ ] Strong + supported matches reviewed for obvious mis-pairings (sample check).
- [ ] No strong match relies on missing reference (Validation_Report check).

## 3. Review queue

- [ ] Every **Review_Queue** / **Rows_Needing_Improvement** row has a `primary_issue_code`.
- [ ] HIGH priority items documented or resolved via manual fields.
- [ ] Unmatched rows classified (missing transaction, duplicate, data quality, etc.).

## 4. AI audit (if AI arbitration was enabled)

- [ ] **AI_Decision_Audit**: every accepted AI match has `validation_status=ACCEPTED`.
- [ ] Rejected AI decisions appear with rejection reason; none silently promoted to matched.
- [ ] Hosted AI was only used with `AI_DATA_APPROVAL=hosted_approved`.

## 5. Balances and special rows

- [ ] Opening/closing balances appear as special rows, not in transaction matching.
- [ ] Final closing balance uses last period balance (not summed sections).

## 6. Team deliverable

- [ ] `team_recon_submission__<pair_id>.xlsx` generated and reviewed.
- [ ] Manual columns intentionally blank for finance team to complete.

## FAIL / REVIEW handling

| Status | Action |
|--------|--------|
| **FAIL** | Do not submit until resolved or explicitly waived with written sign-off |
| **REVIEW** | Human decision required; document in manual fields or external memo |
| **PASS** | Proceed |

## Sign-off record

| Field | Value |
|-------|-------|
| Pair ID | |
| Reviewer | |
| Date | |
| Validation FAIL count | |
| Review Queue open count | |
| AI decisions accepted | |
| Submission approved (Y/N) | |
