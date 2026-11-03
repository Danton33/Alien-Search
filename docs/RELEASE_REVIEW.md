# Release review

Edition 3.3.0 • November 3, 2026

## Package scope

The repository contains the research framework, dated historical findings, 35 public government source descriptors with alternate routes, an assistant guide and starter for ChatGPT, Claude and other assistants, Claude Code instructions and a Claude skill, an optional standard-library Python tool (reads, archive operations, confirmed downloads, safe ZIP handling, user-supplied imports, link checks, optional keyed APIs, local read-only server), JSON/SQLite record storage, documentation, tests, a privacy audit and an allowlisted packager that builds the repository ZIP and the skill ZIP (with its own LICENSE), the same skill in the standard `skills/alien-search/` folder for Agent Skills installers, a GitHub Actions workflow for the offline checks, and hand-run model test cases (docs/EVAL_CASES.md).

## Exclusions and credential handling

- No cloud-drive links, file or folder IDs, sharing metadata, private collection exports, chat or library identifiers, cookies, sessions, account credentials, author names, email addresses, account handles, personal URLs or network addresses.
- No workspace paths, runtime files, cached responses, logs, fetched documents, archive artifacts, Git history, remote, hosted endpoint, reporting service, shared token or API key.
- Each user generates their own local bearer token. Optional GovInfo and National Archives keys belong to each user and never leave their computer except as a header to their own API host.
- The license names only contributors, keeping the earlier project's line as MIT requires.

## Observed checks

- 170 offline tests pass on Python 3.10, 3.11, 3.12 and 3.13 on Linux (one test of Python 3.13's robots.txt parser is skipped on older versions by design). 47 were added in 3.3.0 for the independent review's findings: short and partial responses, resume validation with If-Range, robots.txt and pacing on redirects, percent-encoded robots paths, character sets, literal plain text, `text_status`, cache identity, damaged caches and pause requests kept across upgrades, unique link IDs, fallback order with and without a GovInfo key, archive replay identity, safe and concurrent state writes, damaged indexes and stored files, export permissions and failures, all-or-nothing imports, portable record IDs, ZIP lineage with a member named `manifest.json`, credential redaction, binary-mode file opening on Windows, the Wayback library message, identical steps in the hand-started macOS job, the privacy audit's tracked-file and history checks, and the two identical skill folders. Earlier tests cover key-header stripping, size limits, robots.txt states, ZIP traversal, links, encrypted entries and compression bombs, locked PDFs, archive labelling, fingerprint matching, the National Archives budget, generated-file freshness and the privacy audit.
- An end-to-end run through the command line, with the network faked at the connection, read a Windows-1252 page, reported a download's size, stopped an interrupted download with a resumable error, resumed it with If-Range to the correct SHA-256, extracted a ZIP containing its own `manifest.json`, and matched an imported member to the official download it came from.
- GitHub Actions ran the suite on Windows (Python 3.10 and 3.13) and macOS (3.10 and 3.13). Windows found one problem in a test (two ZIP files left open), now fixed, and both Windows jobs pass; macOS passed. Later macOS runs were cancelled when GitHub had no Mac available, so the macOS job now runs when started by hand.
- One of the eight model test cases in docs/EVAL_CASES.md (the JAL 1628 question) was run with a connected AI assistant on November 3 and passed: correct source, copy labels, no invented IDs, and an honest report of a page it could not open. The other seven cases were not run.
- The release audit found nothing in the 48 allowlisted files. A separate check against the publisher's private identifier list found only a known false match inside a NASA web address. That list stays on the publisher's computer, so others cannot repeat this check. The repository was published as a single commit under a GitHub no-reply address, with a UTC timestamp.
- GitHub CLI 2.95 (`gh skill`) installed the skill's five files from the repository; with the standard folder, `gh skill publish --dry-run` validates it, warning only that `.claude/skills/` looks like installed skills (it is Claude Code's copy, kept on purpose).
- Packaging checks confirm fixed dates and permissions, no archive comment, no host paths, no Git data, and the skill ZIP layout Claude expects (a folder named like the skill containing SKILL.md).

No live government or Internet Archive transport validation was performed for this release, and the National Archives Catalog API response format was not verified against a live key; the tool reads it defensively and says when fields are missing. Historical review notes are not promoted to new validation. No public deployment, publication, account change or background task was performed.

## Remaining limits

Static scanning and tests do not prove the absence of all vulnerabilities. Destinations can see the requesting computer's IP address. GitHub shows the publishing account and commit identity. Some government sites block automated tools entirely; the tool reports that and offers archived or alternate copies rather than working around it. Optional PDF parsers must be kept updated and used in an appropriate isolation boundary.
