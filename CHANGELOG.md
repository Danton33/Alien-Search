# Changelog

## 3.3.1 — November 4, 2026

Patch release for four findings from a coding audit of 3.3.0 (F1, F3, F4, F5). The research method, starter, guide, framework and source register are unchanged, so the model evaluation cases do not need re-running for this release. Bounded continuation for long documents and links (F2) and scoped snapshot-comparison fields are planned for 3.4.0, because they add to the command line and HTTP API.

### Scanned PDFs are no longer reported as fully read (F1)
- A PDF page counts as read only if it yields visible text; a page label alone is not text. An image-only (scanned) PDF now reports `text_status` `none` with no text, and a mix of text and scanned pages reports `partial`.
- PDF results include `pages_with_text` and, when some pages had no text layer, `pages_without_text` (compact ranges such as `1-3, 7`), and `next_steps` tells the assistant not to describe those pages. Applies to live reads, archive snapshots and imported files. OCR is still not performed.
- Cached reads from earlier versions are redone once (cache format 3), so old "complete" labels on scans do not linger.

### A site's pause applies to the whole site (F3)
- An HTTP 429 now pauses every request to that site, whichever source hit it: other sources on the same host, downloads, size checks, link checks, robots.txt and official API calls all wait, and `--refresh` does not override it. `www.` and bare host names share one pause. Other sites stay usable.
- The pause is kept in the state folder (`site-pauses.json`), so separate copies of the tool and the local server honour it. A longer pause is never shortened by a later, shorter one. Internet Archive rate limits feed the same pause.

### Index entries are no longer lost when two operations finish together (F4)
- Download, import and extraction indexes are updated in one locked step (a standard-library file lock that works on Linux, macOS and Windows, across threads and separate processes). Previously two operations finishing at the same moment could each read the old index and one entry would be dropped. Files stay JSON in the same places; atomic writes and damaged-index handling are unchanged.

### Results keep the retry delay (F5)
- Every result that asks the caller to wait now has `retry_after_seconds` and `retry_at` (UTC), plus `retry_after` with the server's own header value when it sent one. This covers command-line errors (which previously dropped the delay), HTTP API errors, reads and archive calls. A rate limit with no Retry-After, or one that cannot be parsed, means one hour, as before.

### Tests
- 20 new tests (`tests/test_v331.py`), including four processes appending to one index at once and a real image-only PDF when PyMuPDF is installed. All 170 earlier tests still pass.

## 3.3.0 — November 3, 2026

Corrective release after an independent review: evidence integrity, safe private state, site rules on redirects and clearer results. The research method and starter structure are unchanged apart from the wording noted below, and the register gains two dated notes.

### Evidence you can trust
- Interrupted downloads resume only when the server confirms the file is unchanged (strong ETag or Last-Modified, sent as If-Range). Without that proof the download restarts, so a saved file can no longer be the start of one version joined to the end of another.
- A resumed response must start where the partial file ends and state the total size, and the finished file must reach that size. A partial range is reported as incomplete and stays resumable.
- A page or file shorter than its declared length is an error, not a smaller document.
- A stored file is reused only if it still matches its SHA-256 name; a damaged copy is replaced.
- Reads report `text_status`: `complete`, `partial` (extraction stopped at its limit) or `none` (format not read, locked PDF or no PDF reader), with matching `next_steps`. `text_truncated` now only means the excerpt is shorter than the extracted text.
- Pages are decoded in their declared character set, or Windows-1252 for older undeclared pages. Plain-text and CSV files are read as text and kept literally, so markers such as `<REDACTED>` no longer disappear (live reads and archive captures alike).
- Files extracted from a ZIP the tool downloaded keep its provenance: `zip-extract` reports the ZIP's `parent`, and importing one of those files reports "bytes match a file extracted from an official download (SOURCE)". Fingerprint matches now name the source they came from.
- The archive replay check (exact timestamp, same original document, matching Memento-Datetime) lives in the archive client itself, so every caller gets it.

### Site rules
- robots.txt and pacing now apply to every redirect destination, not just the first address.
- The robots.txt matcher used on Python 3.10–3.12 normalises percent-encoding as RFC 9309 requires, so `/%70rivate` matches a rule for `/private`.

### Source register
- A021 (JAL 1628) and A035 (National Archives bulk downloads): NARA's bulk-download page lists the JAL 1628 series, 733667, under a Roswell title. Both entries now say so and tell assistants to go by the identifier and description and to list the ZIP's contents first. A021 also records a lead to check, not a verified source: FAA electronic records, 493468575, on the same page. Found during a test run of the method with a connected assistant.

### Order when a source fails
- Failed reads follow the framework's order: official alternates first, then archives, then the user's own download. For sources with a GovInfo package, a user who saved a GovInfo key gets `official_alternate` (the official GPO copy) and no archive request is made. Sources with a UK Government Web Archive copy listed (A022, A023) say to prefer it to the Internet Archive.

### Safe private state
- One shared helper writes every state file: a unique temporary file, flushed and then swapped into place. Interruptions, full disks and two programs writing at once no longer leave empty or half-written files. Damaged caches are rebuilt; damaged indexes are set aside as `NAME.corrupt-TIMESTAMP` instead of crashing the tool.
- Cached reads are redone when the register's URL for a source changes or the parsing rules change.
- `storage.py export --output` replaces an earlier export only once the new one is complete, and the file is owner-only even if the old one was not.
- `storage.py import` checks every record before saving any, names the record it refuses, and with SQLite runs as one transaction. It now prints how many records it imported.
- Record IDs that are Windows device names (`CON`, `NUL`, `COM1` ...) or differ from another only in capitals are refused.
- An empty token file left by an interrupted first start is replaced automatically.
- Windows: every private file is now opened in binary mode. Before this, Windows could turn line breaks inside a downloaded, imported or extracted file into two characters, silently changing its bytes and fingerprint. A test checks every low-level file open.
- The ZIP extraction manifest moved beside the extracted folder (`extracted/<sha256>.json`), so an archive member named `manifest.json` is extracted intact. Older manifests are still read.
- Credential-like values in `import-file --from-url` (tokens, keys, signatures) are replaced with REDACTED before anything is stored or shown.
- Link IDs on a page are checked for uniqueness.
- A site's request to pause is honoured even right after an upgrade. Dropped connections and interrupted or discarded downloads now say `retry: after_wait`, matching their "run it again" advice, and small files are shown in kB rather than "0.0 MB".

### Documents and distribution
- The skill ZIP includes the LICENSE file, as the MIT license requires for copies shared on their own, and `SKILL.md` declares `license: MIT`.
- A generated copy of the skill in the standard `skills/alien-search/` folder lets Agent Skills installers find it without extra flags, for example `gh skill install OWNER/alien-search alien-search --agent claude-code` (also Copilot, Cursor, Codex, Gemini CLI and others). Before this, GitHub's CLI found the skill only with `--allow-hidden-dirs` and warned that it might be another publisher's copy. Tested with GitHub CLI 2.95; `gh skill publish --dry-run` passes. The `.claude/skills/` copy stays for Claude Code, and a test keeps the two identical.
- `SKILL.md` always loads the guide and framework, and searches the source register for the names in a question, reading it in full for broad questions or when fewer than two entries match.
- Assistant guide: facts newer than a model's training are not to be "corrected" from memory, but a current official source that says otherwise is followed, with the change stated. Code sandboxes are told to try once and then use their web tools, rather than assume they cannot reach government sites. New result words: `text_status`, `official_alternate`.
- Framework: archive order prefers a government web archive listed for the source; the tool section mentions redirect checks, safe resuming and `text_status`.
- README: example questions, accurate permission notes for Windows, and the pacing scope. New `docs/EVAL_CASES.md` with eight cases to run by hand in each assistant before a release.
- `.github/workflows/tests.yml` runs the generated-file check, the privacy audit and the tests on Linux (Python 3.10–3.13) and Windows (3.10 and 3.13) for every change, with no secrets and read-only access. A macOS job (3.10) runs when started by hand from the Actions tab, because GitHub's Mac runners were repeatedly unavailable and a job that never starts marks the whole run as failed.
- The privacy audit now also scans every file Git tracks (a tracked file missing from the release list is reported, because GitHub would publish it). `--history` scans every earlier file version, commit message and commit email address before a repository goes public, and reports commits whose time carries a time-zone offset. New patterns cover ChatGPT and Claude share and session links, IPv6 addresses and more key formats (GitLab, Hugging Face, npm, Google OAuth, Stripe, GitHub app tokens). Keys with no recognisable format are still not detected; the private identifier list and GitHub's secret scanning cover more.
- `docs/PUBLISHING.md` adds the history audit, the security settings to turn on after going public, and why to share the packaged ZIPs rather than GitHub's Download ZIP, which carries the commit ID and time.
- Running `python wayback_api.py` directly explains that it is a library and names the `api.py` commands, instead of exiting silently.
- The local API description no longer mentions removed `coverage` fields.

### Corrections to earlier notes
- 3.2.0 said "no loss of function". It removed fixed explanation fields from results and shortened link IDs, which changes output that scripts may have stored; varying evidence fields were kept, and `text_status` now restores the completeness signal that the removed fields only described in general terms.
- 3.2.0 said link IDs were "still unique within a page" without checking; 3.3.0 checks.
- 3.1.1 removed the standalone `wayback_api.py` command line, which was never documented; archive lookups for registered sources remain in `api.py`.
- Pacing and the National Archives monthly count apply within one state folder and one running copy of the tool; earlier notes did not say so.

## 3.2.0 — November 2, 2026

Fewer tokens for AI models, with no loss of function. Measured against 3.1.1 (estimates at about four characters per token): the pasted starter and the full Claude skill are about 6% smaller, `SKILL.md` about 19%, a 40-link page read about 19%, and a full source listing about 77%.

- The assistant guide no longer repeats the evidence labels, review status and copy-provenance lists; it points to framework section 3, where they are defined once. Three rules that were stated in two places now appear once.
- In the starter, the framework no longer repeats the starter's opening lines (the edition, seed date and timezone line stays). The source table and the per-source notes are merged into one entry per source, identical alternate routes are listed once for all the sources that share them, and the generator's hidden section markers are shorter.
- `SKILL.md` drops its "Procedure" section, which repeated `guide.md` and `framework.md`; the always-loaded "Never" list stays.
- `sources` lists ID, title, agency and URL when more than 5 sources match; `--full` (or `full=1` over HTTP) gives every field. Lookups of 5 or fewer sources are unchanged.
- Output is compact when a program or AI agent reads it and indented in a terminal (`ALIEN_SEARCH_PRETTY=1` forces indentation). The local HTTP API also sends compact JSON.
- Explanations that never change between results (what a read covers, archive caveats, provenance reminders) are stated once in the guide instead of in every result. Fields that vary are unchanged.
- Link IDs are 12 characters instead of 20; still unique within a page.

## 3.1.1 — November 2, 2026

Clean-up; no change to research content or behaviour you rely on.

- Removed leftovers from the original creator's private setup: the "preserved snapshots" reader (nothing in this project creates them), an unused `use_engine` setting from a removed third-party engine, an unused attribute, unused imports, the `shared_connector` output field and the undocumented standalone command line inside `wayback_api.py` (`api.py` covers all of it).
- The register, starter, skill and historical findings now say that "retained", "saved" or "preserved" copies belonged to the original reviewer and are not included, so assistants do not promise files users cannot get.
- Fixed two stale details: the local API description no longer hard-codes the source count, and the documented user agent matches the tool.
- The packager now names its outputs `Alien-Search.zip` and `Alien-Search-Skill.zip`.

## 3.1.0 — November 2, 2026

Clearer direction for AI models. Research content is unchanged apart from the register additions below.

- New `docs/ASSISTANT_GUIDE.md`, placed first in the starter and the Claude skill: five always-rules, what to do when the starter is pasted without a question, a table of what to do with or without web access, code execution or the repository, when the method applies, facts newer than many models' training (Department of War name, PURSUE, the FY2025 AARO report, Record Group 615, NASA's 2025 biosignature announcement), one glossary for every status word, a fixed answer template and a worked example.
- The starter separates instructions for the person from instructions for the assistant.
- The source register now shows what was already checked for each source (date, review depth, verified pages, notes), alternate names (for example JAL 1628, Kona Blue, Tic Tac), known gaps such as the 2004 Nimitz encounter, and a NEW-1, NEW-2 convention so models never invent IDs. `sources --query` searches the alternate names, and a search with no match points to the known gaps and the NEW-n convention.
- Tool results now include `next_steps` when something needs doing, and failed reads include the source's alternate copies. Unknown IDs and off-allowlist addresses explain what to do instead.
- The download size check tells the agent to ask the user before `--confirm`, and sizes are in decimal megabytes to match how sites report them.
- `source_as_of_hint` is renamed `register_date_note`, so a register date is not mistaken for the date of a live page.
- The Claude skill's description includes PURSUE, AARO, Project Blue Book and Roswell so it is picked up for those questions; the skill reads the guide first.
- Framework: record fields are for saved records (chat answers use the template); the Python tool section applies only when the user runs it.

## 3.0.0 — November 2, 2026

Renamed to **Alien Search** and reworked for the people who will actually use it: mostly through an AI assistant, sometimes with the optional Python tool. The research framework's findings and sources are unchanged except where noted.

### For assistant users (ChatGPT, Claude and others)
- `ASSISTANT_STARTER.md` replaces the ChatGPT-only starter and works with any assistant. It tells the assistant what to do when a government site is blocked (alternate official copies, then archives, then ask the user), to label where every document came from, to ask before large downloads, and never to ask for keys or get around blocks.
- Claude support: `CLAUDE.md` for Claude Code, a project skill in `.claude/skills/alien-search/`, and a skill ZIP (built by the packager) for uploading the skill in Claude's apps. Project-instruction steps are in the README.
- The starter and skill files are generated from `docs/FRAMEWORK.md` and `sources.json` by `scripts/build_starters.py`, and a test fails if they drift.

### Source register
- Added A035, the National Archives' bulk-download index for UAP-related records.
- Added alternate routes: GovInfo copies for A011 and A012, National Archives bulk and API routes for A001, A002 and A021, the bare war.gov address and file location for A019, UK Government Web Archive links for A022 and A023, and a Government of Canada Web Archive note for A030.
- Added a list of archives to try for blocked, moved or missing pages.

### Framework
- New "copy provenance" labels: official live, official alternate, archived (with capture time), user-supplied (unverified unless its fingerprint matches) and secondary mirror.
- New sections on blocked sources and on responsible use.

### Python tool
- Accepts both `www.` and bare forms of every registered host, so PURSUE files on war.gov are no longer dropped.
- New `download` command: shows the size first, downloads only with `--confirm`, has a 500 MB default limit, checks free space and resumes interrupted downloads.
- New `zip-list` and `zip-extract`: inspect before extracting, extract only what you name, refuse unsafe paths, links, encrypted entries and suspicious compression, and count real bytes against the limit.
- Locked PDFs are labelled instead of being reported as errors; only an empty password is tried.
- `read` attaches a clearly labelled archived copy when the live page is blocked, missing, robots-disallowed or in the wrong format (CLI and HTTP default; `--no-archive-fallback` turns it off).
- New `import-file` for files you download yourself. They stay "user-supplied" unless their SHA-256 matches bytes the tool fetched from the official site or an archive.
- New `verify-sources` link check (headers only) with a dated private report.
- Follows robots.txt (RFC 9309), cautiously on Python versions whose built-in parser is incomplete. Waits 2 seconds between requests to a government site and 5 seconds for the Internet Archive. Reuses archive answers about past periods.
- Optional GovInfo and National Archives Catalog API support with each user's own key, read from standard input, stored owner-only, never printed, and dropped on any redirect to another host. The National Archives queries are counted against the monthly allowance.
- Honest user agent naming the tool as unofficial; an info link is opt-in.
- Results name stored files relative to the state folder, so shared output does not reveal usernames or folder layouts.
- State moved to `~/.local/share/alien-search` (`%LOCALAPPDATA%\alien-search` on Windows); the variable is now `ALIEN_SEARCH_DATA_DIR`. Exports from earlier editions still import.

### Privacy and publishing
- The release audit uses generic patterns only and can check a private, git-ignored list of the publisher's own identifiers without printing them.
- `docs/PUBLISHING.md` explains how to publish without exposing a personal account or email; `docs/RESPONSIBLE_USE.md` covers site etiquette, provenance, reuse terms and private individuals named in records.
- The packager builds both ZIPs with fixed dates and permissions, after checking generated files and running the audit.
- The MIT notice keeps the earlier project's contributor line, as the license requires, alongside this project's.

### Migration
Move or copy your previous state folder to the new default location, or point `--data-dir` at it.

## 2.0.1 — November 2, 2026

Code fixes over 2.0.0.

- Archive snapshots, comparisons and the Wayback page-text command crashed on every real response because of a tuple-order mismatch between the network layer and its callers; tests had mocked the wrong layer.
- Text files are read and written as UTF-8, CLI output is safe on non-UTF-8 Windows consoles, and storage export/import round-trips through PowerShell redirection.
- `--data-dir` no longer changes permissions on an existing folder.
- Failed reads are no longer cached for 24 hours, and Retry-After is capped at 24 hours.
- Non-ASCII Authorization headers get a normal 401; token creation is atomic; both storage backends raise `KeyError` for missing records and close SQLite connections.
