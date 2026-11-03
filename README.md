# Alien Search

An unofficial research toolkit for public government records on UAPs (UFOs), alleged extraterrestrial encounters and the search for life. **It is not affiliated with or endorsed by any government agency.**

It contains a research method that keeps every claim traceable to an identifiable record, a register of 35 public government sources with alternate official copies and archive routes, and an optional Python tool. You do not need any account, key or storage belonging to anyone who wrote or shared it.

## Example questions

Once an assistant has the method, ask it things like:

- "What do government records say about the 1986 Japan Airlines 1628 sighting?"
- "Summarise the newest PURSUE release on war.gov and cite each file you open."
- "Did any government agency ever confirm recovering nonhuman bodies? Separate testimony from official findings."
- "What did NASA say about the 2025 Mars sample, and is it evidence of visitors?"

## Choose how to use it

### ChatGPT or another AI assistant

Paste [ASSISTANT_STARTER.md](ASSISTANT_STARTER.md) into a new conversation, or upload it and ask the assistant to apply it. It holds the full method and source list, and tells the assistant what to do when a government site is blocked. What the assistant can actually do (search the web, open pages, create files) depends on the app and your settings.

### Claude

- **Claude project:** create a project, choose **Set project instructions** and paste the "Assistant guide" section of ASSISTANT_STARTER.md, then add ASSISTANT_STARTER.md to the project's knowledge.
- **Claude skill:** upload `Alien-Search-Skill.zip` (build it with `python scripts/package_release.py`, or take it from the repository's releases) under **Customize > Skills**, using **+**, then **Create skill**, then **Upload a skill**. Skills need code execution turned on in **Settings > Capabilities**. Menu names change from time to time; Claude's help article on using skills has the current steps.
- **Claude Code:** download or clone this repository and start Claude Code in its folder. It picks up [CLAUDE.md](CLAUDE.md) and the project skill in `.claude/skills/alien-search/`.

### Other coding assistants (Agent Skills)

The same skill is in the standard `skills/alien-search/` folder, so the GitHub CLI (version 2.90 or newer) can install it into Claude Code, GitHub Copilot, Cursor, Codex, Gemini CLI and many other assistants. Replace `OWNER` with the account that hosts this repository:

```bash
gh skill preview OWNER/alien-search alien-search                       # read it first
gh skill install OWNER/alien-search alien-search --agent claude-code   # or copilot, cursor, codex, gemini-cli ...
```

It installs from the latest release; `gh skill update` fetches newer releases.

### Python tool (optional)

Python 3.10 or newer, standard library only. Optional PDF text extraction uses an already-installed PyMuPDF (`fitz`); without it, files are still saved and fingerprinted. OCR is not included. No installer, telemetry, login or background jobs.

```bash
python api.py sources --query NASA                  # search the register
python api.py read A019                             # read a source; adds a labelled archived copy if the live page is blocked
python api.py read A035 --kind archive              # list the National Archives bulk ZIPs with their link IDs
python api.py download A035 LINK_ID                 # shows the size only
python api.py download A035 LINK_ID --confirm       # downloads (default limit 500 MB; raise with --max-mb)
python api.py zip-list SHA256                       # look inside a downloaded ZIP without extracting
python api.py zip-extract SHA256 --pdfs             # extract only the PDFs, safely
python api.py import-file A014 ~/Downloads/file.pdf --verify-archive   # add a file you downloaded yourself
python api.py captures A019 --start 2026 --end 2026 --limit 5
python api.py snapshot A019 20261023195253 --max-chars 4000
python api.py verify-sources                        # check that registered links still respond (headers only)
python api.py serve --port 8765                     # local read-only API; see docs/API.md
```

Historical timestamps are locators, not a promise the archive still has them. No original PDFs, archive snapshots, tokens, caches or logs are bundled.

## How it behaves toward government sites

- It only contacts an allowlist of government and archive hosts, over HTTPS, and identifies itself honestly as an unofficial research tool.
- It follows robots.txt, including for every redirect, and waits between requests: 2 seconds per government site and 5 seconds for the Internet Archive. The pause applies within one running copy of the tool; running several copies at once against the same site is not recommended.
- It never tries to get around a block. A blocked, missing or robots-disallowed page is reported as such. It then points to official alternate copies first, and adds a clearly labelled archived copy when one exists.
- Every read says whether the document's text was read completely, partly or not at all, so an assistant never describes text the tool did not read.
- Large files show their size first and download only with `--confirm`. An interrupted download resumes only if the server confirms the file has not changed; otherwise it starts again, so a saved file is never a mix of two versions. ZIP files are listed before anything is extracted; unsafe paths, links, encrypted entries and suspicious compression are refused.
- Files you add yourself stay labelled "user-supplied" unless their SHA-256 fingerprint matches a copy fetched from the official site or an archive, or a file extracted from such a copy.
- Optional GovInfo and National Archives searches use your own free keys, read from standard input and stored privately. They are never printed. See [docs/API.md](docs/API.md).

## Your private data

State lives outside this repository: `~/.local/share/alien-search`, or `%LOCALAPPDATA%\alien-search` on Windows. Choose another folder with `--data-dir` (before the command) or `ALIEN_SEARCH_DATA_DIR`. On macOS and Linux, new folders are made owner-only; an existing folder you point to keeps its own permissions, and a folder anyone can write to is refused. On Windows the default folder is inside your own user profile, which other standard users cannot open; a folder you choose elsewhere keeps that folder's permissions. **Do not use a public, shared or repository folder for state**, and do not sync its `runtime/` folder to the cloud. Outputs refer to stored files by relative names (for example `objects/<sha256>`), so pasting results into a chat does not reveal your folder layout.

## Storage

Reviewed research can be kept as JSON files or SQLite and moved between them:

```bash
python storage.py put CASE_001 --input my-reviewed-record.json
python storage.py get CASE_001
python storage.py export --output my-private-export.json
python storage.py --backend sqlite import --input my-private-export.json
```

These commands only touch your own files. An import is checked in full before anything is saved, and an export replaces the previous file only once it is complete. Record IDs must work as file names on every system, so names such as `CON` and IDs that differ only in capitals are refused. [docs/STORAGE.md](docs/STORAGE.md) explains how to connect other storage.

## More

- [Assistant guide](docs/ASSISTANT_GUIDE.md): what an AI assistant should do first, the answer template, status words and facts newer than many models' training.
- [Research framework](docs/FRAMEWORK.md): source authority, evidence labels, copy provenance and what to do when a site is blocked.
- [Historical findings](docs/INITIAL_FINDINGS.md): dated earlier conclusions, not fresh verification.
- [Source register](sources.json): official URLs, review scope, alternates and archive routes.
- [Responsible use](docs/RESPONSIBLE_USE.md), [Security model](SECURITY.md), [Publishing safely](docs/PUBLISHING.md), [Release review](docs/RELEASE_REVIEW.md), [Changelog](CHANGELOG.md).

Authentic government custody does not establish an underlying claim. Unresolved does not mean extraterrestrial.

## Check and package

```bash
python -m unittest discover -s tests -v
python scripts/build_starters.py --check
python scripts/audit_release.py              # add --history in a full clone before making a repository public
python scripts/package_release.py
```

The packager builds `Alien-Search.zip` (the repository) and `Alien-Search-Skill.zip` (the Claude skill, with its own copy of the license) from an explicit file list, after the privacy audit passes. It never includes Git history, private state, downloads or keys. On GitHub, the same checks run automatically on Linux and Windows for every change (`.github/workflows/tests.yml`). The macOS check runs when you start it from the **Actions** tab (**tests**, then **Run workflow**), because GitHub's Macs are often too busy to take a job; run it before each release. The Python tests cannot check how an AI assistant follows the method; [docs/EVAL_CASES.md](docs/EVAL_CASES.md) lists cases to try by hand before a release.

MIT license for the code and research instructions. Linked government records keep their own rights and conditions.
