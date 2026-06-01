# Reconciliation Engine Hardening (CFO v1)

## Architecture changes

- **Central tolerance policy** (`src/reconciliation/tolerance_config.py`) loaded via `Settings.reconciliation_tolerance_policy()` in `src/config.py`. Global defaults plus conservative per-label overrides (Invoice, Payment, CreditNote, OpeningBalance, etc.).
- **Issue taxonomy** (`src/reconciliation/issue_taxonomy.py`): 23 primary reason codes on every match/review row.
- **Staged deterministic matcher** (`src/reconciliation/matching_engine.py`): 17 deterministic stages before AI/human review, including timing, rounding, credit/debit netting, duplicate detection, and bounded allocation.
- **Allocation engine** (`src/reconciliation/allocation_engine.py`): subset-sum one-to-many, many-to-one, bounded many-to-many (review-aware).
- **Final closing balance** (`src/reconciliation/balance_helpers.py`): last period closing by page/row order, never summed across fiscal sections.
- **Shared matching helpers** (`src/reconciliation/matching_helpers.py`) to avoid circular imports between matching and allocation.

## Matching order (deterministic)

1. Transaction rows only (opening/closing excluded upstream).
2. Exact reference + amount + same-day date + polarity.
3. Exact reference + amount + date tolerance (timing difference).
4. Mutually unique containment reference + amount.
5. Supported unique mirror amount + date + type.
6. Credit/debit note netting.
7. Rounding difference (within per-label rounding tolerance).
8. One-to-many allocation.
9. Many-to-one allocation.
10. Bounded many-to-many allocation (review required).
11. Duplicate reference detection → review.
12. Fuzzy/containment/missing-reference candidates → review.
13. Unmatched org/party with issue classification.

AI arbitration (optional) runs after deterministic stages for unresolved clusters only.

## Issue taxonomy

Primary codes: `ExactMatch`, `TimingDifference`, `MissingTransaction`, `DuplicateTransaction`, `PartialSettlement`, `Overpayment`, `Underpayment`, `TaxDifference`, `CurrencyDifference`, `ReferenceMismatch`, `AccountMappingError`, `CreditNoteIssue`, `DebitNoteIssue`, `BankChargeDifference`, `DiscountDifference`, `PeriodCutOffIssue`, `DataQualityIssue`, `MasterDataIssue`, `IntegrationFailure`, `FraudRisk`, `AmbiguousMatch`, `ManualReviewRequired`, `UnknownException`.

## Tolerance policy

Configure via `.env` (`RECON_*` variables). Per-label overrides are conservative; Opening/Closing balances use zero amount tolerance for matching. Every accepted match records `amount_tolerance_used` and `date_tolerance_days_used` in Match Evidence.

## AI validation

- AI reason slugs mapped to canonical codes via `normalize_ai_reason_code()`.
- Invalid issue codes rejected; low confidence rejected; row IDs and totals post-validated (unchanged safety model).
- Cache keys include config/tolerance version via existing fingerprint pipeline.

## Allocation algorithms

- Candidate blocking by date window, type compatibility, mirror polarity.
- Subset-sum with `max_combination_search_size` cap (default 8).
- Unique solution required except many-to-many (always review).

## Limitations

- No cross-currency FX matching (classify as `CurrencyDifference` only when evidence exists).
- No OCR in this release; vector/text/table extraction only.
- Manual override columns are write-blank; reruns do not ingest Excel overrides automatically.
- Many-to-many only when exactly one bounded solution exists; otherwise review.

## Migration notes

- Match rules renamed (`stage5_containment_ref_amount`, `stage15_no_ref_amount_date`, etc.).
- Review Queue and Match Evidence include `primary_issue_code` and manual resolution columns.
- Closing balance validation uses final-period balance, not sum of all closing rows.
