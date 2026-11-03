# Security and privacy model

The release is built from an explicit file allowlist, never from a copy of a working folder. It contains no Git history or remote, private collections, cloud-drive links or IDs, workspace paths, account exports, API keys, tokens, logs or fetched files. No author name, email address, account handle, network address, personal URL or hosted endpoint is included; the license names only "contributors". The privacy audit checks this on every build and every test run, covering every release file and every file Git tracks, including any personal identifiers the publisher lists privately; `--history` also checks earlier versions, commit messages and commit email addresses.

Nothing in the code contacts or depends on anyone who wrote or shared the project. There is no telemetry, reporting service, cloud writer, OAuth flow, browser-session access, account probing, automatic installer or scheduled job, and no AI-provider API call. Built-in storage makes no network calls.

## Boundaries

- **Local server:** binds 127.0.0.1 only; a fresh per-installation bearer token protects every route except `/health`; Host names other than localhost are rejected; any browser Origin is rejected and no CORS permission is sent; access logs are off; the server header omits the Python version. Token comparison is constant-time on bytes.
- **Outbound requests:** only allowlisted government hosts and the Internet Archive, over HTTPS on the default port, to validated public addresses on pinned sockets (no second DNS lookup), with certificate checks and at most five checked redirects. No ambient cookies, credentials or proxy settings are used. robots.txt is honoured and requests are paced, for the first request and every redirect. A response shorter than its declared length is rejected, and an interrupted download resumes only when the server confirms the file is unchanged.
- **Optional API keys:** read from standard input or an environment variable, stored owner-only, never printed, cached or written to outputs, and sent only to their own API host. A redirect to any other host drops the key header.
- **Identification:** the user agent names the tool and says it is unofficial. An info link is added only if the user sets `ALIEN_SEARCH_INFO_URL`, because a link can identify whoever publishes it.
- **Untrusted content:** responses are size-bounded; scripts on pages are never run. ZIP files are listed before extraction; absolute paths, `..` paths, links, encrypted entries and suspicious compression ratios are refused, and actual bytes written are counted against the limit. Locked PDFs are labelled, never cracked. PDF parsing (optional PyMuPDF) is not a sandbox: use an updated parser and an isolated environment for documents you distrust.
- **Private state:** lives outside the checkout with owner-only POSIX permissions for everything the tool creates. A folder chosen with `--data-dir` keeps its existing permissions and is refused if anyone can write to it. A shared OS account, administrator, malware or cloud-sync agent may still read state; Windows users should check folder access settings.
- **Outputs:** refer to stored files by relative names, so sharing results does not reveal usernames or folder layouts.
- **Scope:** the local HTTP server is for one trusted person on one computer. It is not hardened for public traffic or denial-of-service. Do not forward its port.

## What cannot be guaranteed

A clean repository does not make publishing or browsing anonymous. GitHub shows the publishing account, commit author, email and timestamps; follow docs/PUBLISHING.md. Government sites and archives see the IP address and timing of every request the tool makes. The audit is heuristic: it cannot prove that no future change adds a secret or personal detail. Run it again after every change. Git ignore rules do not remove anything already committed; if a credential is ever published, revoke it at the issuing service and clean the repository history.

## Reporting a vulnerability

Use the publishing repository's private security advisory feature if its owner enables it. Do not put real credentials or private data in a public issue. No personal reporting address is embedded.
