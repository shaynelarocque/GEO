# The four lenses

Every program audit applies the same four lenses, in this order. Each lens is anchored against the **goal_anchor**: the program's own stated objectives, original budget, success metrics, and timeline (see `goal-extraction.md`). Every verdict measures the program against itself, never against external opinion.

Each lens emits structured output via the `set_lens` tool. The fields are: `verdict` (green / yellow / red / insufficient_evidence), `evidence_tier` (strong / moderate / limited / n/a), `summary` (one sentence ≤ 140 chars for the header card), `key_numbers` (3-5 quantitative anchors), `rationale_md`, `counter_argument_md`. The budget lens additionally emits `budget_tranches`. The tier rubric is defined in the system prompt and applies uniformly across all four lenses; do not improvise tier criteria.

## 1. Stated Objectives

**Question:** Has what the program *says* it does shifted, expanded, contracted, or hollowed out since founding?

**Evidence to seek:**
- Founding-document language (press release, Treasury Board submission, first Departmental Plan).
- Current language (latest Departmental Plan, recipient annual report, contribution agreement amendments).
- Whether amendments to a contribution agreement changed the `expected_results_en` or `prog_purpose_en` text or left it unchanged while the dollars moved.

**Verdict guide:**
- **green**: current stated objectives match founding language; targets are still measurable. Tier should be `strong`.
- **yellow**: language has softened or broadened ("drive adoption" became "support adoption"); measurable targets dropped or replaced with engagement metrics.
- **red**: the program's stated mission has changed without a public re-mandating, OR the original measurable targets are no longer mentioned. Needs at least `moderate` tier with a primary or hansard source.
- **insufficient_evidence**: no founding-document language found.

**Key numbers to surface:** examples for this lens include "Founding language hash vs current hash", "Amendments retaining original wording: N of M", "Years since last Departmental Plan mention".

## 2. Budget

**Question:** Did the program stay near its original budget envelope, and how did it grow?

**Evidence to seek:**
- Original budget commitment (founding announcement, Treasury Board submission, first contribution agreement).
- Amendment chain on the contribution agreement (`fed.grants_contributions` grouped by `agreement_number`, ordered by `amendment_date`). The time series IS the budget story.
- Each amendment's `additional_information_en` field, which often contains the explicit reason for the change.
- Public Accounts of Canada: actual disbursements vs. authorized.

**Mandatory structured output:** every `set_lens` call for `budget` includes `budget_tranches` covering the founding commitment, every amendment, and the latest authority. Each tranche carries `{label, date, amount_cad, note?, source?}`. The frontend renders this list as a horizontal trajectory ribbon at the top of the budget card with the latest-to-founding ratio called out at the rightmost node. Do not generate raw chart code; emit the structured list.

**Verdict guide:**
- **green**: actuals within ~110% of original commitment; no amendment creep.
- **yellow**: moderate growth (110-150%) with disclosed reasons; new commitments are publicly justified.
- **red**: material growth (>150%) without a public re-mandating; multiple amendments raising value with vague `additional_information_en`; unfunded scope expansions absorbed quietly.
- **insufficient_evidence**: neither founding-document budget nor amendment chain visible.

**Key numbers to surface:** "Original budget", "Latest authority", "Ratio (latest / founding)", "Number of amendments", "Total disbursed". Five anchors max.

## 3. Adoption

**Question:** Is the program achieving the user, usage, or uptake outcomes it promised?

**Evidence to seek:**
- Founding-document KPIs (e.g. "X% of physicians using the system by year 5").
- Recipient annual reports and their published metrics.
- Departmental Results Reports.
- Auditor General reports, committee testimony.
- Industry data, peer-reviewed evaluation, third-party measurement.

**Verdict guide:**
- **green**: primary_gov or structured_dataset sources show the program met or is on track to meet its stated KPIs. Tier `strong`.
- **yellow**: partial progress, missed milestones with disclosed reasons, KPIs revised mid-flight with explanation.
- **red**: published outcomes well below stated KPIs; the program stopped reporting against original metrics; AG or committee has documented adoption shortfalls.
- **insufficient_evidence**: adoption metrics are not in primary_gov sources. **This gap usually triggers an ATIP draft or an OPQ draft.** See `instruments.md`.

**Key numbers to surface:** "Stated target (year)", "Latest reported value", "Gap to target", "Years since last public KPI report".

## 4. Vendor

**Question:** Who are the suppliers under this program, and is the relationship competitive or concentrated?

**Evidence to seek:**
- Federal grants and contributions disclosures (the recipient).
- Federal contracts (procurement disclosures: `canadabuys.canada.ca` or the recipient's own procurement).
- Recipient annual report: vendor list, IP arrangements.
- Committee testimony naming vendors.
- Sole-source justifications.

**Verdict guide:**
- **green**: vendors disclosed, competitive procurement, no IP concentration that would orphan public investment.
- **yellow**: single dominant vendor with disclosed competitive process; some IP retained by vendor with public licensing.
- **red**: sole-source dominance, undisclosed vendors, or IP retained by a vendor in a way that prevents the Crown from continuing the program independently (the PrescribeIT pattern).
- **insufficient_evidence**: vendor identity or relationship not in primary_gov sources. **Trigger ATIP draft.**

**Key numbers to surface:** "Primary vendor", "Vendor share of program spend", "Vendor IP retention (%)", "Number of competing vendors in last competition".

## Sequence and self-skeptic

Run lenses in this order. For each one:
1. Gather evidence (DB + web). Score sources by tier per `provenance.md`.
2. `self_assess` with what you've learned (tagged with the lens phase).
3. Propose a rationale.
4. Generate the strongest **counter-argument** a defender of the program could make.
5. `set_lens` with the structured output.
6. After every lens, ask whether any earlier lens needs revision. If yes, call `set_lens` again on that earlier key and `record_pivot` to log the direction change.

A lens with no counter-argument recorded is incomplete. A lens whose verdict caps higher than the tier permits (e.g. green with `limited` tier) is invalid; the renderer will show a tier mismatch warning.
