# Provenance scoring

Every claim you record in a lens cites at least one source. Score each source by tier. The tier sets the weight you give the claim when you decide a verdict and a confidence band.

## Tiers (highest to lowest)

1. **primary_gov**: Documents the Government of Canada published about its own program. Treasury Board submissions, Departmental Plans, Departmental Results Reports, Public Accounts of Canada, founding announcements on canada.ca, the Canada Gazette, an Auditor General report, contribution agreements published under proactive disclosure. The program speaking on its own record.
2. **structured_dataset**: Authoritative datasets the government or the hackathon organizers published or curated. The shared Postgres (`cra`, `fed`, `ab`, `general`) is the main one. Treat individual rows as primary evidence for what they encode (a federal grant of $X to recipient Y on date Z), but not for narrative claims.
3. **hansard_committee**: Parliamentary record. Hansard transcripts, committee testimony, Order Paper questions and government responses, committee reports. Public officials speaking under privilege; high evidentiary value, with political framing.
4. **established_press**: Reported journalism from outlets with editorial standards and a public correction record (CBC, The Globe and Mail, La Presse, Postmedia chain, Bloomberg, Reuters, The Logic, The Hub, Policy Options). Use for narrative and triangulation.
5. **partisan_press**: Outlets with a clear ideological orientation (Rebel News, Press Progress, etc.). Cite only when nothing else has the fact, and label the orientation in your rationale.
6. **unverified**: Social media, blogs, press releases from interested parties, anything you could not anchor in a higher tier. Use as a lead. Never as load-bearing evidence.

## Verdict rules

- A **green** verdict needs at least two converging primary_gov or structured_dataset sources.
- A **red** verdict needs at least one primary_gov, structured_dataset, or hansard_committee source. Press alone supports a red rationale, never a red verdict.
- If your only evidence for a claim sits at established_press or below, the verdict caps at **yellow**.
- If you cannot anchor a claim above unverified, do not make the claim. Flag the gap and trigger a drafted accountability instrument.

## What "primary" means

Primary evidence answers three questions: what did the program say it would do, what did it spend, what did it deliver? The program saying it on its own record is primary. A journalist reporting that the program said it is established_press, even when the journalist quoted accurately. When you can chase a press claim back to the underlying gov doc, do. Cite the gov doc as primary, with the press article as the path you took.
