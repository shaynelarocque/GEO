# Goal extraction: the accountability anchor

Every audit begins by extracting what the program said it would do, what it would cost, and how it would be measured. This becomes the **accountability anchor**. Every later judgment measures the program against its own claims, never against external opinion.

If you cannot extract a credible goal anchor, every downstream lens floats. Spend real effort here.

## Fields to fill on `GoalAnchor`

- `stated_objectives`: one or two paragraphs of the program's mission language, ideally quoted from the founding document, with a citation.
- `original_budget`: human-readable: "$X over Y years from Department of Z, originally committed YYYY-MM-DD". Cite the founding document.
- `success_metrics`: bullet list of *measurable* targets the program committed to. Adoption percentages, cost per outcome, milestones with dates. If the founding doc only contains aspirational language, list it but mark `metric_kind: aspirational` in the rationale.
- `timeline`: original start, original end, key milestone dates.
- `sources`: every founding document you used, scored per `provenance.md`. At least two primary_gov citations is the bar for a complete anchor.

## Where to look (in order)

1. **The hackathon-shared Postgres `general` schema first.** Resolve the program or recipient name to a canonical entity. Pull `general.vw_entity_funding` for total funding, span, departments. Pull `general.entity_source_links` joined to `fed.grants_contributions` for the actual grant rows. The fields `prog_purpose_en`, `expected_results_en`, `description_en`, `agreement_title_en` often contain the program's own goal language at the time the agreement was signed. The earliest non-amendment row is the closest thing the dataset has to "founding language for this funding stream." See `database-cookbook.md`.

2. **Founding announcement.** A press release on canada.ca (search `site:canada.ca "<program name>" announcement`) or the recipient's own newsroom. Quote the dollar figure and timeline. Watch for "up to $X over Y years" framing. The *up-to* word matters.

3. **Treasury Board submission summary.** Rarely public for a specific program, but the Departmental Plan that follows it is usually public.

4. **First Departmental Plan after launch.** Each department publishes one yearly on canada.ca. The plan that introduced the program states objectives and indicators. Departmental Plans live at `https://www.canada.ca/en/<department>/corporate/transparency/...` patterns and at GC InfoBase.

5. **First Departmental Results Report after launch.** The year-end accountability counterpart of the Plan. Frequently states the original KPIs.

6. **Recipient's first annual report after the program launched.** When funding flows through a third party (Infoway, a Crown corporation, an arms-length nonprofit), their first annual report usually restates the goal language.

7. **Founding contribution agreement.** Sometimes published; sometimes only available via ATIP. If the agreement is not public, that is itself a finding worth flagging.

## Anti-patterns to flag in the goal anchor itself

- Goal language that is purely aspirational with no measurable targets. Record it, and flag in the rationale that the program was launched without measurable success criteria, which makes every downstream verdict structurally provisional.
- "Up to $X" framing. Note that the headline figure is a ceiling, not a commitment.
- Program scope expressed only in process terms ("convene stakeholders", "support development") with no outcome metric. Note the gap.
- Goal language identical to a different program's language. Note the boilerplate; do not dignify it as program-specific.

## Output discipline

Write the goal_anchor before opening any lens. Do not start the Stated Objectives lens until the anchor exists, because the lens IS a comparison of current language against the anchor. If you cannot extract an anchor, stop the audit and surface that as the finding.
