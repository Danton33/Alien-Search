# Alien Search research framework

Government records on UAPs, alleged extraterrestrial encounters, and the search for life.
Edition 3.3.0 • Research seed dated November 2, 2026 • Default research timezone: UTC.
Alien Search is an unofficial research method. It is not affiliated with or endorsed by any government agency.

## Start here

Alien Search finds, preserves and evaluates government-provided records relevant to extraterrestrial life and alleged nonhuman activity. It welcomes unresolved evidence while requiring every interpretation to remain traceable to an identifiable record. The source register holds 35 public locators with historical review notes and, where known, alternate official copies. Original PDFs and private collection metadata are not bundled. Review depth varies and is recorded for each item.

## 1. Research lanes

- UAP observations: civilian and military sightings, sensor records, aviation encounters, physical traces and case resolutions.
- Programs and disclosure: investigations, intelligence assessments, congressional testimony, budgets, contracts, declassification decisions and records-management history.
- Life beyond Earth: biosignatures, astrobiology, SETI and technosignatures. Keep microbial-life evidence separate from claims of intelligent visitation.

Begin with the United States and verified foreign government archives. The starter set includes U.S., UK, French and Canadian documents, with a Brazilian archive locator. This is a growing collection, not a census of every government record.

## 2. Source authority and claim authority

Use original records on official government, military, parliamentary, government archive or national space-agency sites. Retain government-released third-party documents, but identify their actual authorship. A document in government custody can contain a newspaper clipping, unsolicited letter, speculative proposal or unsupported witness statement.

Distinguish three questions: Is this an authentic government-held record? What does its author actually claim? What evidence supports the underlying event or interpretation? Official publication answers the first question more readily than the other two.

Use secondary sites only to discover primary document URLs or identify disagreements requiring investigation. Do not promote a secondary summary or a privately re-hosted copy to an official finding. Preserve conflicting government conclusions with dates and scope rather than selecting a preferred authority.

## 3. Evidence labels

- Documented administrative fact: a program, hearing, release, record transfer or disposal is supported by the relevant record.
- Reported observation: the record contains a witness or instrument report. Identify firsthand, secondhand, contemporaneous or retrospective reporting.
- Official analytical finding: a named agency or investigator reached a stated conclusion; retain methods, confidence and limitations.
- Unresolved — insufficient information: the evidence cannot support identification or a performance assessment.
- Unresolved — investigated: an investigation retained an unexplained case. This is stronger support for continued inquiry than a missing-data label, but does not identify an extraterrestrial origin.
- Potential biosignature or technosignature: a scientific candidate requiring alternative explanations and independent confirmation.
- Extraterrestrial origin established: reserve for publicly inspectable evidence sufficient to establish origin, with independent corroboration and competing explanations addressed. No item receives this label in the starter findings.

Record review status separately: locator only; index reviewed; selected sections reviewed; full document reviewed; scan needs transcription; or blocked. A saved PDF is not automatically a reviewed PDF.

### Copy provenance (where the bytes came from)

Record this for every document you rely on. It is separate from the evidence label.

- Official live copy: fetched from the agency's own site.
- Official alternate copy: another government host or official service (for example a GPO GovInfo copy or a National Archives copy of another agency's record).
- Archived copy: a web-archive capture. Always give the archive, the exact capture time and the original URL. An archived copy shows what a page said at that moment, not what it says today, and a later capture cannot prove earlier availability.
- User-supplied copy: a file the user obtained and provided. Treat it as unverified unless its SHA-256 fingerprint matches bytes fetched from the official site or from an archived capture. A matching fingerprint proves the bytes are identical; it does not make the contents true.
- Secondary mirror: a copy on a non-government site. Use it only to find the official original.

## 4. Repeatable search

- Start with source indexes and exact case/program names. Search synonyms including UAP, UFO, flying disc, flying saucer, unidentified aerial/anomalous phenomena, nonhuman, extraterrestrial, biosignature and technosignature. Use French PAN and Portuguese OVNI where relevant; exclude immigration-law uses of "alien."
- Resolve the original agency, document number, archive series and record identifier before writing a finding. Search both current official pages and archival catalogs.
- Retrieve originals when permitted; record failed paths and redirect behavior. Preserve the original bytes and compute SHA-256 to identify duplicates and later changes.
- Extract a bounded finding with a verified page/section locator. Open image-only pages before quoting or adopting details. Check names, units, time zones, report/event counts and alternative explanations.
- Link related reports to the same incident. Cross-posts, official mirrors, archive captures and repeated testimony from the same underlying source are not independent corroboration.
- Update the same source entry and finding when new evidence arrives. Preserve the previous conclusion and explain the revision.
- Be gentle with public servers: fetch one item at a time, reuse what you already have, and do not crawl whole sites.

## 5. When a source is blocked, moved or missing

Work through these in order and record which route produced the document:

1. Alternate official copies listed for that source in the register (another government host, the GovInfo or National Archives services, or the same site under its other address).
2. Archived copies: first a government web archive listed for that source (the UK Government Web Archive or the Government of Canada Web Archive), then the Internet Archive Wayback Machine (which replays the U.S. End of Term crawls of .gov and .mil sites) or the Library of Congress Web Archives.
3. Ask the user to open the official page in their own browser and download the file themselves, then label it a user-supplied copy.

Never try to get around a block: no CAPTCHA solving, logins that are not the user's own, disguised browser identities, unblocking or proxy services, or ignoring robots.txt. A block is recorded as a block.

## 6. Historical recovery with archives

Use archives when an official page is missing, moved, changed or needed as of a past date. The archived government original retains its original authority and limitations; the archive is only the preservation host.

Record the original URL, replay URL, exact UTC capture time, fetch time, document identity, coverage limits and any Memento-Datetime header. Keep event date, publication date, capture date and retrieval date separate. Use the right MIME filter: application/pdf for PDFs and text/html for webpages. A bounded index query does not establish complete archive coverage.

Validate the actual response bytes and document cover. A replay returning an HTML viewer is not the PDF. A capture does not prove its factual claims or current availability.

## 7. Minimum record fields

These fields are for records you save. In a chat answer, use the answer template in the assistant guide instead of listing every field.

Source ID; incident ID if applicable; title; originating agency/author; releasing custodian; country; event date; document date; public release date when verified; information cutoff; classification/release markings as shown; original URL; copy provenance; archive locator and capture time; fetch timestamp; user-owned storage reference (keep private); SHA-256; page count; evidence type; firsthand/secondhand status; reviewed pages; exact finding; agency conclusion; alternative explanations; uncertainty; related records; next action.

Leave unknown fields explicitly unknown. Never infer classification, release date or provenance from a filename alone. Keep case count, report count, sightings, witnesses and sensor detections distinct. Do not calculate "alien probabilities" from the fraction of unidentified reports.

## 8. Storage and updates

Use the current user's chosen storage. Default to portable JSON, Markdown and unchanged source bytes in a private folder outside the repository. Read docs/STORAGE.md for the provider-neutral contract. OCR, translations and annotations remain separate. Deduplicate by bytes and source lineage without merging distinct versions. Never publish private storage references, file paths, API keys or tokens.

On a new run, inspect this framework and the current register; check meaningful new releases; collect changes; add page-specific findings and limitations; retain revision history. No recurring task, FOIA request, external message, spending, account change or publication is authorized by the framework itself.

## 9. Acquisition with the optional Python tool (only if the user runs it)

Skip this section unless the user has the repository on their own computer or you are working in it (for example in Claude Code). The repository's api.py reads registered sources, lists their links, checks file sizes, downloads with your confirmation, lists and extracts ZIP files safely, imports files you downloaded yourself, looks up archive captures and compares them, and checks that registered links still respond. Fetches remain candidate evidence; failures are quarantined, not retried around. It honours robots.txt and waits between requests, including on redirects, and resumes an interrupted download only when the server confirms the file is unchanged. Results say whether a document's text was read completely, partly or not at all (`text_status`). Large downloads always show their size first and need confirmation. Every result has a `class` and, when something needs doing, `next_steps`; the assistant guide explains them. Optional GovInfo and National Archives searches need the user's own free keys, which stay on their computer; never paste a key or token into a chat. No account with the original authors is needed.

## 10. Responsible use

- Do not present this tool or its outputs as official, or imply government endorsement.
- U.S. federal government works are generally public domain, but records can contain third-party material, and other countries' records can carry reuse terms. Check before republishing documents in bulk; linking to the official source is always fine.
- Keep private research state, downloads and keys out of public repositories and chats.

## 11. Next research priorities

PURSUE item-level inventory; the FY2025 Virginia maritime report; Trans-en-Provence sampling and controls; Valensole original police statements; Nimitz and FAA JAL 1628 records; original Go Fast and ORNL reports; selected Blue Book unexplained files; Robertson Panel records; Brazilian item-level records; primary papers behind biosignature candidates.
