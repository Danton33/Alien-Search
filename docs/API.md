# Python tool, local API and credentials

Alien Search's `api.py` is a user-owned, unofficial tool over public sources. Public government and archive reads need no key. Nothing connects to, reports to or depends on anyone who wrote or shared the project.

## Commands

Run `python api.py --help` for the full list. `--data-dir` goes before the command.

| Command | What it does |
| --- | --- |
| `sources` | Search the saved register (not a government-wide search). When more than 5 sources match, it lists ID, title, agency and URL only; add `--full` for everything |
| `read ID` | Read one registered page, PDF or text file: hash, metadata, text, discovered links and `text_status` (complete, partial or none). When the live page is blocked, missing, robots-disallowed or in the wrong format, it adds `official_alternate` (the GovInfo copy, if the source has one and you saved a GovInfo key); otherwise a labelled `archived_copy` (turn off with `--no-archive-fallback`) |
| `download ID [LINK_ID]` | Report the file size; download only with `--confirm`. Default limit 500 MB (`--max-mb`). Interrupted downloads resume only when the server confirms the file is unchanged (strong ETag or Last-Modified, sent as If-Range); otherwise they restart |
| `zip-list SHA256` | List a downloaded ZIP without extracting; flags unsafe entries |
| `zip-extract SHA256 [NAMES] [--pdfs]` | Extract only the named files (or all PDFs) into the private state folder, with size limits. The result's `parent` says where the ZIP came from; extracted files keep that provenance |
| `import-file ID PATH [--from-url URL] [--verify-archive]` | Add a file you downloaded yourself. It stays "user-supplied" unless its SHA-256 matches a copy this tool fetched from the official site or an archive, or a file extracted from one; the match names its source. Credential-like values in `--from-url` (tokens, keys, signatures) are replaced with REDACTED before anything is stored or shown |
| `captures`, `closest`, `snapshot`, `compare` | Internet Archive capture discovery, exact capture reads and two-capture comparison |
| `verify-sources [--ids A001,A002]` | Check registered links with headers only and save a dated private report |
| `set-key govinfo` / `set-key nara` | Store your own optional API key (read from standard input, never the command line) |
| `govinfo ID` | Optional: find the official GPO copy of a source through the GovInfo API |
| `nara-search QUERY` | Optional: search National Archives catalog descriptions |
| `serve --port 8765` | Local read-only HTTP API |

Every result is JSON with `ok` and `class` (ok, input, auth, blocked, missing, drift, robots, rate, limit, network or error; docs/ASSISTANT_GUIDE.md explains each). When something needs doing, `next_steps` says what, and failed reads include the source's `alternates`. `register_date_note` repeats the register's dating note; it is not the date of the page you just read. Sizes are in decimal megabytes (1 MB = 1,000,000 bytes). Output is indented when you run a command in a terminal and compact when another program or an AI agent reads it, to save tokens; set `ALIEN_SEARCH_PRETTY=1` to force indentation. Explanations that never change between calls (what a read covers, archive caveats, provenance reminders) are stated once in docs/ASSISTANT_GUIDE.md rather than in every result; anything that differs between results, such as `text_status`, is in the result itself.

Results refer to stored files by names relative to the state folder (for example `objects/<sha256>`, `extracted/<sha256>/file.pdf`), never by full paths.

## Network behaviour

- Destinations: registered government hosts (each in its `www.` and bare form) and the Internet Archive, over HTTPS on the default port, at validated public addresses, with up to five checked redirects. Proxy environment variables and browser cookies are not used, so networks that require a proxy will block the tool; use the assistant starter or a browser instead.
- Identification: `AlienSearch/3.3 (unofficial public-records research)`. Setting `ALIEN_SEARCH_INFO_URL` to an https page adds that link, which helps site operators but also identifies whoever runs that page, so it is off by default.
- robots.txt is checked before each government page or file, and again for every redirect destination. If it disallows the path, the result is class `robots`. If robots.txt cannot be read because of a server or network error, the standard says to treat the site as disallowed for now; the tool rechecks after 10 minutes. On Python 3.13 the standard library's parser is used; on Python 3.10–3.12, whose parser misses some of the standard's rules, a cautious reading is used that may block a few allowed pages but never the reverse.
- Pacing: at least 2 seconds between requests to the same government host (including redirect hops) and 5 seconds between Internet Archive requests. Pacing is kept within one running copy of the tool; separate copies started at the same time do not share it, so run one at a time against the same site.
- Completeness: a response shorter than its declared length is an error, not a smaller document. Pages are decoded in their declared character set (or Windows-1252 for older undeclared pages); plain-text files are kept literally, so markers such as `<REDACTED>` stay visible.
- Caching: successful reads 24 hours (redone at once if the register's URL for that source changes or the tool's parsing rules change; damaged cache files are rebuilt); blocked, missing, drift and robots results 15 minutes; network and server errors are retried on the next call; rate limits are honoured until the server's Retry-After passes, capped at 24 hours. Archive answers about periods that are already over are reused for a week.
- Size limits: page and PDF reads 20 MiB; downloads 500 MB by default (up to 20 GB with `--max-mb`), with a free-space check; ZIP extraction 2 GB by default.

If the Internet Archive blocks you, the tool stops asking until its Retry-After passes. The Archive asks people who think they were blocked in error to email info@archive.org.

## Optional keys

Both services are free; each person requests and uses their own key. The tool works fully without them.

- **GovInfo** (U.S. Government Publishing Office): an api.data.gov key from GovInfo's API page. Used by `govinfo` and `download ID --via govinfo` for sources that list a GovInfo package.
- **National Archives Catalog API**: request a key by email from the National Archives at Catalog_API@nara.gov. The default allowance is 10,000 queries per month; the tool counts its own queries in your state folder and stops at that number (the count is per state folder, and two copies of the tool running at the same moment can each count the same query once) (change with `ALIEN_SEARCH_NARA_MONTHLY_LIMIT`). Results carry NARA's notice that the product uses its API but is not endorsed or certified by NARA. NARA deactivates keys that are posted publicly.

Store a key with `python api.py set-key govinfo` (or `nara`) and paste it when prompted; it is saved owner-only in the state folder's `runtime/` directory. You can instead set `ALIEN_SEARCH_GOVINFO_KEY` or `ALIEN_SEARCH_NARA_KEY`. Keys are sent only to their own API host; if a response redirects elsewhere, the key header is dropped. Never paste a key into a chat, issue, screenshot or file in the repository.

## Local HTTP API and its token

`python api.py serve` generates a random 256-bit bearer token on first use, stores it owner-only in `runtime/api-token.txt` in your state folder and reuses it. The server listens only on 127.0.0.1, rejects other Host names and any browser Origin, and sends no CORS permission. All routes except `/health` need the token. To rotate it, stop the server, delete that file and start again.

Local client example (run on the same computer, outside any chat):

```python
import json, os, pathlib, urllib.request
if os.name == 'nt':
    state = pathlib.Path(os.environ['LOCALAPPDATA']) / 'alien-search'
else:
    state = pathlib.Path.home() / '.local/share/alien-search'
token = (state / 'runtime' / 'api-token.txt').read_text(encoding='ascii').strip()
req = urllib.request.Request('http://127.0.0.1:8765/v1/sources?q=NASA', headers={'Authorization': 'Bearer ' + token})
print(json.load(urllib.request.urlopen(req)))
```

| Path | Function |
| --- | --- |
| `/health` | Minimal local health response |
| `/openapi.json` | Local API schema |
| `/v1/sources` | Filter the saved register |
| `/v1/sources/{id}` | Source descriptor, including alternates |
| `/v1/sources/{id}/read` or `/links` | Bounded read with optional `archive_fallback=0/1` (default 1) |
| `/v1/sources/{id}/documents/{link_id}` | Inspect a link discovered on that source (20 MiB limit) |
| `/v1/sources/{id}/captures`, `/closest`, `/snapshots/{timestamp}`, `/compare` | Archive operations |

Downloads, ZIP handling, imports, link checks and keys are command-line only, so the HTTP API stays read-only. It is meant for one person on one computer: do not expose its port, forward it or put it behind a tunnel.

## Assistants and the tool

Pasting ASSISTANT_STARTER.md, or using the Claude skill, gives an assistant the method and source list; it does not let a cloud assistant reach a program on your computer. Run the tool yourself and share its output, or use Claude Code on your own computer, where Claude can run it directly.
