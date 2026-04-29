# Database cookbook

Read-only SQL against the hackathon-shared Postgres via the `query_db(schema, sql)` tool. Schemas: `cra`, `fed`, `ab`, `general`. The tool enforces SELECT or WITH only, blocks statement chaining, caps row count, and applies a statement timeout.

Trim queries with `LIMIT` aggressively while exploring. Aggregate before listing. Paste rows back as evidence with `source` formatted as `<schema>.<table>:<pk>=<value>` so the user can audit the row.

## Resolve a program or recipient name to a canonical entity (always do this first)

```sql
SELECT id AS entity_id, canonical_name, bn_root, dataset_sources,
       fed_profile, cra_profile, ab_profile,
       jsonb_array_length(aliases) AS alias_count
FROM general.entity_golden_records
WHERE canonical_name ILIKE '%' || :search || '%'
   OR aliases::text ILIKE '%' || :search || '%'
ORDER BY array_length(dataset_sources, 1) DESC NULLS LAST,
         (fed_profile->>'total_grants')::numeric DESC NULLS LAST
LIMIT 10;
```

If the program name does not match an entity directly, search for the recipient that administers the program ("Canada Health Infoway" rather than "PrescribeIT"). The `recipient_hint` field on the audit input is for exactly this case.

## One-shot funding profile for an entity

```sql
SELECT *
FROM general.vw_entity_funding
WHERE entity_id = :entity_id;
```

Returns CRA, FED, and AB totals, counts, earliest and latest dates, and a `total_all_funding` summing across datasets. This is the **goal-anchor candidate** for `original_budget` if the audit is about the recipient as a whole, with one caveat: a program is usually a *slice* of a recipient's funding, not the entire envelope.

## Pull every federal grant linked to an entity (with goal-extraction fields)

```sql
SELECT g._id, g.ref_number, g.agreement_number, g.amendment_number, g.is_amendment,
       g.agreement_value, g.agreement_start_date, g.agreement_end_date, g.amendment_date,
       g.prog_name_en, g.agreement_title_en,
       g.prog_purpose_en, g.expected_results_en, g.description_en,
       g.additional_information_en
FROM general.entity_source_links sl
JOIN fed.grants_contributions g ON g._id = (sl.source_pk->>'_id')::int
WHERE sl.entity_id = :entity_id
  AND sl.source_schema = 'fed'
ORDER BY g.agreement_start_date, g.amendment_number NULLS FIRST;
```

This is the **workhorse query for an Infoway-style audit.** The text fields contain the program's own goal language at the time each agreement was signed.

## Amendment-creep on a single agreement

```sql
SELECT amendment_number, amendment_date, is_amendment, agreement_value,
       agreement_value - LAG(agreement_value) OVER (ORDER BY amendment_number) AS delta_from_prev,
       agreement_end_date,
       additional_information_en
FROM fed.grants_contributions
WHERE agreement_number = :agreement_number
ORDER BY amendment_number NULLS FIRST;
```

Watch for `additional_information_en` boilerplate: "the total agreement value previously disclosed has been updated to reflect an increase resulting from this amendment". The Government's own euphemism for amendment creep with no narrative justification.

## Recipient by business number (catches BN suffix variants)

```sql
SELECT _id, recipient_legal_name, recipient_operating_name,
       agreement_number, amendment_number, agreement_value,
       agreement_start_date, prog_name_en
FROM fed.grants_contributions
WHERE recipient_business_number LIKE :bn_root || '%'
ORDER BY agreement_start_date, amendment_number NULLS FIRST;
```

Use the `bn_root` from `general.entity_golden_records` to catch every variant suffix (RR, RC, RP).

## Sanity-check: language drift across amendments

```sql
SELECT amendment_number, agreement_value,
       md5(coalesce(expected_results_en, '')) AS results_hash,
       md5(coalesce(prog_purpose_en, '')) AS purpose_hash
FROM fed.grants_contributions
WHERE agreement_number = :agreement_number
ORDER BY amendment_number NULLS FIRST;
```

If every hash is identical while `agreement_value` moves, the dollars grew while the stated objectives on paper did not. Material for the Stated Objectives lens.

## CRA: govt funding share for a registered charity

```sql
SELECT *
FROM cra.govt_funding_by_charity
WHERE bn_registration LIKE :bn_root || '%'
ORDER BY fiscal_year DESC;
```

Useful when the recipient is a registered charity and you want to apply the "Zombie Recipients" framing (>70-80% of revenue from public sources).

## CRA: directors of a charity (governance and related-parties material)

```sql
SELECT bn_registration, fiscal_year, director_name, position, related_party_flag
FROM cra.cra_directors
WHERE bn_registration LIKE :bn_root || '%'
ORDER BY fiscal_year DESC;
```

## Reminder

These queries are starting points. Compose new SQL when you need to. The `query_db` tool returns column names alongside rows, so introspect first if a table is unfamiliar:

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = :schema AND table_name = :table
ORDER BY ordinal_position;
```
