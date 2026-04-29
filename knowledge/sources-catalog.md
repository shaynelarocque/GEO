# Sources catalog

Where to look for what. Use this as a checklist; reach for the highest-tier source available for each claim.

## Structured datasets: the hackathon-shared Postgres

Connection comes from `DATABASE_URL` in `.env`. Read-only replica. You query it via the `query_db(schema, sql)` tool.

| Schema | Contents | Start here when… |
|---|---|---|
| `general` | Cross-dataset entity resolution. ~853K canonical organizations, each with aliases, source links, and per-dataset profiles. Views: `vw_entity_funding` (one-shot funding totals), `vw_entity_search` (search). | You have a program name and need to find the canonical recipient entity, OR you want a one-shot funding profile across all datasets. |
| `fed` | 1.275M federal grants and contributions, 51 departments, 422K recipients. Single workhorse table: `fed.grants_contributions`. | You need actual grant rows: agreement values, amendment chains, program purpose and expected results language at the time of signing. |
| `cra` | 7.3M T3010 charity filings, plus pre-computed loop detection, SCC, overhead-by-charity, govt-funding-by-charity. | The recipient is a registered charity, OR you suspect circular flows of funding between nonprofits. |
| `ab` | Alberta open data: grants, blue-book contracts, sole-source contracts, non-profit registry. | The recipient or vendor has an Alberta footprint, or you want sole-source patterns in AB-specific procurement. |

See `database-cookbook.md` for ready-to-run SQL.

## Primary government sources (web)

| Source | URL pattern | Use for |
|---|---|---|
| Departmental Plan / Departmental Results Report | `https://www.canada.ca/en/<department>/corporate/transparency/...` | Goal anchor; Adoption lens; Stated Objectives drift over years. |
| Open Government portal | `https://open.canada.ca` | Cross-checking funding figures; finding the underlying dataset behind a Departmental Plan number. |
| GC InfoBase | `https://www.tbs-sct.canada.ca/ems-sgd/edb-bdd/` | Department, program, and result alignment; expenditure breakdowns. |
| Public Accounts of Canada | search "Public Accounts of Canada Volume III <year>", typically PDFs | Actual disbursements vs. authorized; payments to specific recipients. |
| Auditor General of Canada | `https://www.oag-bvg.gc.ca` | Performance audits. The AG's findings are top-tier evidence. |
| Founding announcements / press releases | `site:canada.ca "<program name>" announce` | Goal anchor; original budget language. |
| Canada Gazette | `https://gazette.gc.ca` | Regulations, OICs, formal program creation. |

## Parliamentary record

| Source | URL pattern | Use for |
|---|---|---|
| Hansard | `https://www.ourcommons.ca/DocumentViewer/...` (debates) | Ministers' statements about the program, opposition questions, on-record commitments. |
| Standing committee transcripts | `https://www.ourcommons.ca/Committees/en/<acronym>/...` | Detailed program testimony from officials and recipients. Highest-density signal for adoption and vendor lenses. |
| Order Paper questions | `https://www.ourcommons.ca/PublicationSearch/...` | Numerical answers to specific questions from MPs. Often pre-empts the need for an ATIP. |
| Senate committee transcripts | `https://sencanada.ca` | Often more substantive on policy detail than House committees. |

## Established press

CBC, The Globe and Mail, La Presse, Postmedia chain, Bloomberg, Reuters, The Logic, Policy Options, The Hub, The Walrus, IRPP. Use for narrative and to find the trail back to a primary source.

## Recipient-published

Annual report, governance disclosures, audited financial statements, news, blog. Treat as the recipient speaking on its own record. Useful for vendor and adoption lenses, with the caveat that the recipient is describing itself, not an external check.

## When a source is missing

If your highest-tier source for a load-bearing claim sits at established_press or below, that is a *gap*. The right response is not to lower the standard. The right response is to draft an accountability instrument (ATIP, OPQ, committee follow-up) addressed to the role that holds the answer. See `instruments.md`.
