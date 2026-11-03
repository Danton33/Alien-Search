# Publishing safely

The ZIP is ready to extract into a new, empty GitHub repository. It contains source files, instructions, public source URLs and tests. It is not a hosted deployment and is not posted anywhere automatically.

## Protect your identity before the first commit

1. **Decide which account the public will see.** A public repository always shows the account that owns it. If you do not want this project linked to your personal profile, create a separate GitHub account or organization for it.
2. **Set a private commit email.** In GitHub's email settings, turn on keeping your email address private and copy the address it gives you (it ends in `users.noreply.github.com`). Then, inside the new repository folder:
   ```bash
   git config user.name "A name you are happy to publish"
   git config user.email "PASTE-YOUR-GITHUB-NOREPLY-ADDRESS"
   git config --get user.email   # check before committing
   ```
   Uploading files through GitHub's website also records your account, so set the email preference there as well.
3. **List your own identifiers privately.** Create `private_input/identity-terms.txt` (this folder is git-ignored and never packaged) with one item per line: your name, usernames, email addresses, the names of your computer's user folder and any private project names. Then run `python scripts/audit_release.py`; it names any release file containing one of them without printing the term.
4. **Keep the info link off** unless you want site operators to see a page about the project. `ALIEN_SEARCH_INFO_URL` is not stored in the repository, but a page you link can identify you.
5. **Commit in UTC.** Every commit records its time with your time-zone offset, which hints at where you live. On macOS or Linux, commit with `TZ=UTC git commit ...` (or `export TZ=UTC` first); in Git Bash on Windows, run `export TZ=UTC` first. Edits made on GitHub's website record your browser's time zone, so make changes with Git if that matters to you. The history audit (below) reports any commit with an offset.
6. **Do not add** screenshots of your desktop, terminal output showing your folders, exported chats, cloud-drive links, API keys, the `runtime/` folder or downloaded documents.

## Publish

1. Extract the release into a new, clean folder. Run `python -m unittest discover -s tests`, `python scripts/build_starters.py --check` and `python scripts/audit_release.py`.
2. Review `release-files.json`. Add only those files to the new repository; never upload a parent folder, `private_input/`, state, downloads or exports.
3. Commit and push with the identity set above. The archive deliberately contains no commit history, remote or author identity.
4. **Push to a private repository first** and open its **Actions** tab. The included workflow runs the tests on Linux and Windows for every change. To include macOS, open **tests** and click **Run workflow**; it runs only when started by hand because GitHub's Macs are often unavailable, and a Mac that never arrives would otherwise mark the run as failed. Make the repository public once the tests pass, and run the macOS check before each release.
5. **Before making it public, audit the history.** In a full clone (no `--depth`), run `python scripts/audit_release.py --history`. It scans every earlier version of every file, every commit message and every commit's email addresses, and fails if the clone is shallow. Anything it finds is already in the history, so deleting the file is not enough: start a fresh history instead (for example one commit containing the current files) while the repository is still private.
6. **Right after making it public**, open the repository's **Settings**, then its security settings: turn on **private vulnerability reporting** (SECURITY.md points people to it) and check that **secret scanning** and **push protection** are on. GitHub turns these on automatically only for repositories created public.
7. **Release:** create a release and attach both ZIPs built by `scripts/package_release.py`: `Alien-Search-Skill.zip` (the Claude skill, with its own copy of the license) and `Alien-Search.zip`. Point people to these rather than GitHub's **Download ZIP** button or the "Source code" archives GitHub adds to every release; those are fine to leave, but they carry the commit ID, commit time and repository name, while the packaged ZIPs carry no Git metadata.

After making changes, run `python scripts/package_release.py`; it checks the generated starter files, runs the audit and builds both ZIPs in the system temporary folder (or a folder you name). Never zip the whole working folder.

The workflow in `.github/workflows/tests.yml` needs no secrets or cloud credentials, has read-only access to the repository, never contacts government sites, and uses only GitHub's own `checkout` and `setup-python` actions, pinned to exact commits. It runs the generated-file check, the privacy audit of every release and tracked file (generic rules only; your private identifier list stays on your computer) and the tests. Delete the file if you do not want automatic checks.

## Credit and license

The MIT license requires keeping the copyright and permission notice in copies, including the skill ZIP, which therefore carries its own LICENSE file. The LICENSE file keeps the earlier project's contributor line alongside this one; neither line names a person.
