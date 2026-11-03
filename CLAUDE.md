# Alien Search — notes for Claude Code

This repository holds an unofficial research method and a standard-library Python tool for public government records on UAPs and the search for life. It is not affiliated with or endorsed by any government agency.

## Where things are

- For research questions, start with `docs/ASSISTANT_GUIDE.md` (always-rules, answer template, status words, facts newer than your training), then `docs/FRAMEWORK.md` and `sources.json`.
- Generated from those three (do not edit by hand): `ASSISTANT_STARTER.md` and `.claude/skills/alien-search/guide.md`, `framework.md`, `sources.md` and `LICENSE` (copied from the root LICENSE), and the whole `skills/alien-search/` folder (the standard Agent Skills copy that installers such as `gh skill install` find; `SKILL.md` is edited only in `.claude/skills/alien-search/`). After editing a canonical file, run `python scripts/build_starters.py`.
- Project skill: `.claude/skills/alien-search/SKILL.md` describes the research procedure; use it for research questions.
- Tool: `python api.py --help`. Modules: `api.py` (reads, archives, CLI, local server), `acquire.py` (downloads, ZIPs, imports, link checks), `robots.py`, `official_apis.py` (optional keyed APIs), `secure_http.py`, `storage.py` (records, and the shared safe-write helpers every module uses for state), `wayback_api.py`, `common.py` (result envelope, text decoding, `text_status`).

## Rules for working here

- Private state lives outside the repository (`~/.local/share/alien-search`, or `%LOCALAPPDATA%\alien-search` on Windows). Never copy state, downloads, tokens or API keys into the repository or into your replies, and never print files from the state folder's `runtime/` directory.
- Keep the network etiquette intact: robots.txt checks, pacing, the host allowlist and size limits. Do not add code that bypasses blocks, disguises the user agent as a browser, solves CAPTCHAs or routes through proxy or unblocking services.
- Every tool result has `class` and often `next_steps`; follow them. Never add `--confirm` to `download` until the user agrees to the size it reported.
- Keep it standard-library only. PyMuPDF stays optional.
- Write private state only through `storage.write_json`, `storage.atomic_write` and `storage.store_object`, and read it with `storage.load_json`, so every file stays safe against crashes, concurrent writers and damage.
- Privacy: never add names, email addresses, personal URLs, local paths or account identifiers to repository files. The publisher can list their own identifiers in `private_input/identity-terms.txt` (git-ignored) so the release audit catches them.

## Checks

- Tests: `python -m unittest discover -s tests`
- Before publishing: `python scripts/audit_release.py` (and `--history` in a full clone before a repository goes public), then `python scripts/package_release.py`, and run the macOS job by hand (Actions > tests > Run workflow)
- After changing the starter, guide, framework or skill: run the cases in `docs/EVAL_CASES.md` in the assistants you support
