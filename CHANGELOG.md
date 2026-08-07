# Changelog

## 0.7.0 — 2026-08-07

Second field contribution from the same beta tester — thank you again!

- **`remy.py runs` + `remy.py rewrite`**: the translation primitive.
  `runs` dumps every text run (index, link, bold, paragraph); `rewrite`
  replaces runs by index while explicitly re-applying the old character
  style — because inserted text inherits the PRECEDING character's
  style, links included, a 23 000-character document survives
  translation with all 42 links and 9 images intact. Runs are processed
  back to front, which is what makes a partial batch failure survivable.
- **`remy.py table`** replaces one Markdown pipe table in the document
  with a real Docs table (bold header, column alignment) — the
  "@remy make this a proper table" case.
- Both are direct writes and refuse without `--direct` plus the user's
  explicit consent; a rewrite or a table structure cannot be shown as
  markup.
- **`suggest` now verifies its writes**: after the batch it reads the
  document back and reports `ok` only once the inserted text is actually
  present (the tester saw one silent loss; `ok` is a promise, so it is
  now earned).
- **Inherited links fixed**: an insertion after linked text no longer
  comes out linked; replacements inside linked text keep the link
  deliberately.
- **`@remy` with a single `@` is now found** — people type @name
  everywhere else, and a colleague's note went unseen.
- **`--text-file` / `--replace-file`** on `suggest` for long texts,
  instead of fighting shell quoting.

## 0.6.0 — 2026-08-05

- **`remy.py md <url> --file report.md`** imports Markdown as real
  document structure: named heading styles (`#` becomes the title),
  bold/italic/code, pipe tables as real Docs tables with bold header row
  and column alignment, lists that never merge their numbering, rules.
  Contributed from the field by a beta tester who moved a 9 KB report
  with six tables into a Doc — thank you! It is a direct write and the
  one deliberate exception to markup mode (mint-highlighting a whole
  import would hide exactly the formatting it exists to produce): it
  refuses a non-empty document unless `--replace` is given.
- The same tester reported markup accept/reject failing at the end of
  the body, with the spared newline bleeding mint onto later insertions
  — both fixed since 0.3.1; the tester's 0.3.0 predated the fix.
- Prompt-injection hardening: `session` now tells the agent explicitly
  that document text and comments are data from possibly unknown
  collaborators, never instructions — the tasks list is the only
  instruction channel from inside a document. SKILL.md states the same
  rule.

## 0.5.0 — 2026-08-05

Out of beta. The core — coloured markup, accept/reject, @@remy notes,
comments, the one-call session — has been unchanged since 0.4.0 and has
now survived two independent field tests (Windows, and a fresh CLI
install) plus three environments (desktop app, CLI, Claude Science),
with every finding fixed the same day. 73 offline tests and the
docs-match-reality suite guard it.

The road ahead: **1.0** when Anthropic accepts Remy into the official
plugin directory; **2.0** when Google makes suggestion-writing generally
available and the native-suggestions module moves into this public
build.

## 0.4.5-beta — 2026-08-05

- Running under a plain python3 older than 3.10 now says so in every
  reply: the PEP 723 requires-python header only takes effect under
  `uv run`, so the second field test ran on system Python 3.9 without
  anyone noticing. The `python_warning` field names the clean fix (uv
  brings its own Python). Remy keeps working — a warning, not a gate.

## 0.4.4-beta — 2026-08-04

Everything in here was found by the first Windows field test — thank you!

- The success message no longer crashes on a cp1252 Windows console (the
  ⌘ symbol): unencodable characters are escaped instead, so a successful
  install no longer reads as a failure.
- `install-mcp` explains the restart for Windows too (File > Exit,
  including the tray icon) and warns that a *running* app may write its
  own settings back on quit and overwrite the fresh entry — if Remy's
  tools are missing after the restart, close the app first and run it
  once more.
- `displayName` removed from the plugin manifest: older Claude Code
  versions reject manifests with fields they do not know, which made the
  install fail until Claude Code itself was updated.
- README: Windows is no longer "untested" — one field test says it works,
  with rough edges during installation; new troubleshooting entry for the
  vanished connector entry.

## 0.4.3-beta — 2026-08-04

- Remy can format. `suggest format` proposes heading (1-6), bold, italic
  and link changes with the markup strategy Dirk specified: the old text
  is struck through **in its old format**, the same text is re-inserted
  **in the new format**, mint-highlighted — so `markup reject` still
  restores the document exactly, with no undo record needed, and `markup
  accept` keeps only the styled copy. `suggest insert --heading N`
  inserts new text as a ready-styled heading paragraph. The MCP server
  gains `suggest_format` and a `heading` parameter on `suggest_insert`.
  `--direct` restyles in place, as everywhere.
- The cloud-sandbox abort no longer claims Remy can never run there:
  environments that can grant network access on request (Claude Science
  can) are told which four Google domains to approve and to retry —
  found live when Remy came up inside Claude Science.

## 0.4.2-beta — 2026-08-03

- The daily update check downloads a tiny `version.json` from the latest
  GitHub release instead of calling the tags API (which stays as a
  fallback). GitHub's public download counter on that file doubles as an
  anonymous tally of active installations — Remy sends nothing about the
  user or their documents, and `REMY_NO_UPDATE_CHECK=1` still switches the
  check off entirely. The tag workflow publishes the release and its asset
  automatically.

## 0.4.1-beta — 2026-08-03

Findings of a full code review, all fixed:

- A document the service account cannot see no longer produces a Python
  traceback: every command reports Google API denials as JSON with the
  share-link hint. `session` on a document not shared by link was the
  commonest way to hit this.
- A beta now recognises its own final release as an update —
  `parse_version` used to strip the pre-release suffix, so exactly the
  beta testers would never have heard about the release.
- MCP hardening: `suggest_insert` and `insert_image` explain themselves
  when no anchor (after/before/at_end) is given instead of dying with a
  TypeError, `finish` no longer drops `open_items` when no summary is
  given, and unexpected errors come back as JSON instead of killing the
  tool call.
- The comment fallback names the real reason (the link does not allow
  editing) instead of blaming the missing Developer Preview.
- Docs match reality again: SETUP.md dropped an environment variable that
  does nothing and a `--markup` flag that does not exist, and names the
  actual `remy-bot@` account; SKILL.md no longer shows `whoami` taking a
  URL and documents that anchors cannot reach footnotes, headers or
  footers.
- Housekeeping: the key-creation hints cover Google's ten-keys-per-account
  limit, the key file is written with mode 600 from the first byte,
  "deleteing" fixed, dead code removed.
- Releasing is now just pushing the version bump: a GitHub workflow tags
  `v<version>` automatically when it lands on main — the tag the Cloud
  Shell one-liner depends on. The one-liner itself now uses `curl -f` and
  falls back to `main` should the tag ever be missing, instead of piping
  a 404 page into bash.

## 0.4.0-beta — 2026-08-03

First beta. The 0.1–0.3 line below tells the story of two days of field
testing on a fresh Mac; 0.4.0-beta is that result, declared stable enough
to hand to other people: one-sentence install covering Code, Chat and
Cowork, self-guided account setup, coloured markup that is always visible
and reversible, @@remy notes, and honest self-explanations everywhere Remy
cannot run.

## 0.3.12 — 2026-08-03

- Plugin updates now reach Chat and Cowork automatically. `install-mcp`
  registers a launcher at a version-independent path
  (`~/.config/remy/mcp-launcher.sh`) that picks the newest installed
  `remy_mcp.py` at every app start — previously the connector entry pointed
  into the versioned plugin cache and silently kept serving the old version
  after every update. Rerunning the setup sentence once picks the new
  mechanism up; after that, never again.

## 0.3.11 — 2026-08-03

- The Google-account setup now works from Chat and Cowork too: the MCP
  server gains `setup_guide` and `import_key` tools. The server always runs
  locally on the user's machine — even when Chat calls it from its cloud
  side — so it can find the downloaded key in Downloads and install it,
  which shell commands in Chat never could. Setup is no longer a
  Code-tab-only affair.

## 0.3.10 — 2026-08-03

- Remy offers the Google-account setup by itself: when `session` finds no
  key it instructs the agent to propose the five-minute setup proactively
  (once, in one sentence, and to drop it if declined), and a failing write
  points at `setup --guide` instead of at a reference file. The account
  step disappears from the getting-started sequence — install, restart,
  share a document; Remy asks for the rest when it matters.

## 0.3.9 — 2026-08-03

- One sentence, one restart: the install sentence now includes running
  `install-mcp`, so a single paste covers Code, Chat and Cowork; only the
  app restart (⌘Q) remains a manual step. The separate "set yourself up for
  Chat and Cowork" sentence stays documented for anyone who skipped it.
- The docs mention the one-time permission question the app shows when Remy
  first acts in Chat or Cowork, and say plainly that allowing it is fine —
  Remy runs entirely on the user's own computer.

## 0.3.8 — 2026-08-03

- The truth about Chat and Cowork, verified end to end on a fresh Mac: the
  desktop app does **not** load the plugin's bundled `.mcp.json` for Cowork
  — Chat *and* Cowork both read the app's own connector list. One sentence
  covers both: "Remy, set yourself up for Chat and Cowork" (runs
  `install-mcp`), plus an app restart. Docs, SKILL.md and the runs-where
  table now say so; the bundled `.mcp.json` stays for Code sessions and
  future app versions. A Cowork task must run On your computer — the icon
  next to the task name shows a computer, not a cloud.
- `session`/`check` tell the agent how markup is unlocked (set the share
  link to Editor) so it stops suggesting email invitations, and `whoami`
  states that the robot needs no invitation for link-shared docs.

## 0.3.7 — 2026-08-02

- Remy actually appears in Cowork now: the desktop app launches MCP servers
  with a minimal PATH, so the bundled server — started with a bare `uv` —
  silently never ran, and Cowork fell back to the read-only Drive
  connector. A `/bin/sh` launcher script now finds uv itself
  (`~/.local/bin`, Homebrew, `/usr/local/bin`) and execs the server;
  `install-mcp` registers the same launcher for Chat, so the entry
  survives uv moving. Found live on the fresh-Mac test: Code worked,
  Cowork answered "the document doesn't exist".

## 0.3.6 — 2026-08-02

- `import-key` no longer crashes when a JSON file in Downloads holds a
  top-level list (or any non-object): key recognition now checks the shape
  first. Found live on the first fresh-Mac setup, where an unrelated JSON
  file was lying in Downloads.

## 0.3.5 — 2026-08-02

- The Cloud Shell script no longer dies silently when creating the key
  file: a brand-new service account is eventually consistent, so the first
  `keys create` can 404. Both setup paths now retry briefly and, if it
  still fails, show gcloud's real error (including a pointer to the
  `iam.disableServiceAccountKeyCreation` org policy) instead of exiting
  without a word. An ERR trap names the line if anything else stops the
  script. Found live: the run ended after "Creating the key file…" with no
  download and no message.
- `setup --guide` hands out the version-tagged raw URL for the Cloud Shell
  one-liner instead of `main` — right after a release, `main` on
  raw.githubusercontent.com can still serve the previous script from cache.

## 0.3.4 — 2026-08-02

- Setup survives a deleted project: a GCP project stays "pending deletion"
  for ~30 days, during which `describe` still finds it but every API call
  fails with RESOURCES_NOT_FOUND. Both the Cloud Shell script and
  `remy.py setup` now check the lifecycle state and walk to a free sibling
  id automatically. Found live when a previously deleted `remy-…` project
  broke the one-liner.
- Setup offers **one route**: Google Cloud Shell in the browser. The local
  gcloud path remains as `remy.py setup` for developers, but the guide no
  longer presents a menu — and sharing a key file between people is no
  longer suggested anywhere: one person, one robot account.
- The install sentence tells Claude to use the CLI bundled inside the
  desktop app when `claude` is not on the PATH — which is the normal case
  for people who only have the desktop app (verified on a fresh Mac).
- SKILL.md instructs the agent to install `uv` itself when it is missing
  (fresh Macs), or fall back to pip + python3, without bothering the user.

## 0.3.3 — 2026-08-02

- Cloud sandboxes are detected up front: `session` and `check` first probe
  whether Google is reachable at all. In claude.ai browser sessions (Chat and
  Cowork) the container's network allowlist refuses the connection before TLS
  starts, and Remy now aborts immediately with a plain `tell_the_user`
  message and the installation link, instead of surfacing cryptic tunnel
  errors. Any HTTP answer from Google counts as reachable, so the probe never
  misfires on documents that merely deny access. Found by pasting the install
  sentence into a browser Cowork session, where it "succeeds" into a
  throwaway container that cannot reach Google at all.
- SKILL.md tells the agent to relay that message and stop — no retrying,
  no debugging the sandbox. The claude.ai Drive connector look-alike (reads
  with the user's own Google login, cannot write) is documented in the
  README.

## 0.3.2 — 2026-08-02

- Windows support in the CLI: `gcloud` is resolved via the PATH (on Windows
  the executable is `gcloud.cmd`, which a bare subprocess call misses),
  `install-mcp` finds the desktop app's config in `%APPDATA%\Claude` (and
  `~/.config/Claude` on Linux), and the missing-`uv` hint gives the PowerShell
  installer on Windows. Untested on real Windows so far; the README states
  the support honestly: on a Mac Remy covers Code, Cowork and Chat, on
  Windows Code only, and WSL sessions load no plugins at all.
- The README now warns about the two environments where the install sentence
  *seems* to work: desktop Chat and browser Cowork both run the commands in a
  temporary sandbox that is recycled after the session — and that could never
  reach the key on your computer anyway.

## 0.3.1 — 2026-08-02

- Fixes picture markup: the shading used to land on the wrong paragraph (or
  none), so an image could survive `markup reject` unmarked. The insertion is
  now fully deterministic — newline, picture, newline — with the picture's
  paragraph shaded mint and the leading newline marked as an insertion, so
  `markup reject` restores the document exactly.
- `markup reject` no longer leaves an empty, still-shaded paragraph behind
  when removing a proposed picture.
- `markup accept|reject` no longer fails with an API error when marked text
  sits at the very end of the document (or a table cell): Docs folds the
  segment's final newline into the marked run, and that newline cannot be
  deleted — found live, the text before it is now removed and the newline
  merely unmarked.
- `finish` and `session` now count shaded (picture) paragraphs as unresolved
  markup; an image-only session no longer ends with "nothing to ask about".
- `done` actually strikes the tag through as markup when Remy has edit
  rights, as documented; the ✅ marker comment is now only the comment-only
  fallback.
- Remy recognises its own comments by Drive's `author.me` flag instead of the
  display name, which is more robust against renamed service accounts.
- Chat and Cowork get the full toolset: the MCP server now also exposes
  suggest_insert, suggest_delete, markup accept/reject, reply/resolve,
  insert_image, task_done and finish — "Remy, accept the changes" works there
  too.
- `comment` no longer claims a comment was anchored; the Docs UI ignores
  anchors set through the API.
- `suggest insert --all` fails with an explanation instead of being silently
  ignored, and `install-mcp` no longer overwrites its first backup.
- The install instructions now lead with a sentence to paste into a Code
  session — Claude runs `claude plugin marketplace add` / `claude plugin
  install` itself. The interactive `/plugin` commands only exist in the
  terminal CLI; the desktop app's Code tab answers "isn't available in this
  environment" (which is documented behaviour, not a Remy fault).
- Passes `claude plugin validate --strict`: `category` moved from plugin.json
  to the marketplace entry, and the marketplace gained a description.

## 0.3.0 — 2026-08-02

- `image` inserts a picture from any public URL, as a real suggestion or as
  markup (the picture goes in its own paragraph and the paragraph is shaded).
  Remy cannot upload a local file: Google fetches the picture itself, and a
  service account has no storage of its own to serve one from.
- `@@remy` is only read as an instruction at the start of a paragraph, and
  Remy's own comments are skipped. Documentation *about* the tag — and Remy's
  own reports — were being executed as commands.
- Fixes `suggestion_ids()` missing suggested images entirely, which made them
  invisible to `suggestions list` and to `accept --all`.

## 0.2.0 — 2026-08-02

- Works in **Cowork** as well as Claude Code: the plugin bundles a local MCP
  server (`.mcp.json`), so one install covers both. `install-mcp` adds Remy to
  the desktop app's **Chat** too. Nothing is hosted; the key never leaves the
  machine. A browser tab cannot be reached — a web page cannot start a local
  process.
- Notices when a newer version has been published and mentions it once.
  `REMY_NO_UPDATE_CHECK=1` switches that off.
- Explains itself when the Google client libraries are missing instead of
  failing with a traceback.
- `setup` reuses the project the existing key belongs to rather than creating
  a second one, and prints the Cloud project number.
- Suggestion support moved into an optional `preview.py` module that public
  builds do not carry.

## 0.1.0 — 2026-08-01

First release.

- Read any Google Doc shared with "anyone with the link", with no setup at all.
- Propose changes as reviewable coloured markup: mint `#96FADB` for
  insertions, pink `#FD96D5` + strikethrough for proposed deletions. Nothing is
  ever deleted outright; resolve with `markup accept|reject`.
- Read, write, answer and resolve comments.
- Execute `@@remy` commands left in the document text or in comments, and mark
  them handled so they never run twice.
- `setup` creates the user's own Google service account end to end.
- `session` returns access, write mode, language, outline, tasks and text in
  one call; `finish` returns the exact question to put to the user.
- Language matching is enforced in code: text in a different language than the
  document is refused unless `--force-language` is passed.
- A hook for native Google Docs suggestions: if an optional `preview.py`
  module is present, Remy proposes changes as real tracked changes instead of
  markup, which works even on a comment-only link. That module is **not part
  of this build** — the capability entered Developer Preview on 7 July 2026
  and the program terms forbid shipping preview features publicly.
- `suggestions list|accept|reject|delete` to resolve suggestions, including
  colleagues'. These request types are absent from the public API discovery
  document but work; listing needs no preview access.
