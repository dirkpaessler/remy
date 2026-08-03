# Remy setup

## Which Claude modes Remy works in

| | how it gets there |
|---|---|
| **Claude Code** | the skill, installed with the plugin |
| **Cowork** | local MCP server, bundled with the plugin |
| **Chat** (desktop app) | say *"Remy, set yourself up for Chat"* once, then restart the app |
| **claude.ai in a browser** | not possible — a web page cannot start a local process |

Everything runs on your own machine. Nothing is hosted, and the key below
never leaves the computer it was created on.

## If you don't use a terminal

You don't need one. Just paste your Google Docs link into Claude and say what
you want done — when Remy needs write access it will walk you through it and
run everything itself. There is deliberately one route:

**In the browser, nothing installed.** Open
<https://shell.cloud.google.com>, sign in with your Google account, and
paste the single line Claude gives you. About a minute later a file called
`remy-key.json` downloads. Go back to Claude and say *"I downloaded the key
file"* — it finds the file and puts it in place. That terminal window is
the only thing you have to touch, because it lives inside Google's own web
page where Claude cannot reach.

Nothing here costs money, and you can skip it entirely: without a key Remy
still reads and discusses documents, it just can't write in them.
(Developers who already use gcloud: the terminal section below automates
the same thing locally.)

## The short version, for the terminal

```bash
gcloud auth login                      # one browser window, once
uv run scripts/remy.py setup           # does everything else
uv run scripts/remy.py import-key      # or: install a key you were given
```

`setup` creates a Google Cloud project, enables the Docs and Drive APIs,
creates a service account called `remy-bot`, and writes its key to
`~/.config/remy/service_account.json` with mode 600. It is idempotent — run it
again any time; it will tell you if a key already exists.

If `gcloud` is missing it says so and stops:
`brew install google-cloud-sdk` on macOS, otherwise
<https://cloud.google.com/sdk/docs/install>.

The rest of this file explains what that does and how to do it by hand.

## Why a service account at all

Google's APIs have no anonymous write path. Reading a link-shared document
needs no identity, but anything that changes a document — a comment, a
suggestion, coloured markup — must be signed by *some* Google account.

A service account is the least invasive way to provide one: it is a robot
account with an empty Drive of its own, it sees only documents already shared
by link, and it never touches your personal Google account. The alternative,
OAuth against your own account, would give Remy access to your entire Drive
and require a browser login flow.

# Doing it by hand — service account in ~10 minutes

Remy reads link-shared docs with **no setup**. To let Remy *write*
(suggestions + comments), it needs its own Google identity: a **service
account**. That is a robot Google account represented by a JSON key file —
no browser login, no OAuth flow, ever.

Because your doc is shared as "anyone with the link can comment", the service
account can work on it without being invited. It sees **only** link-shared
docs; its own Drive is empty. Small blast radius by design.

Two paths — pick one.

## Path A: gcloud CLI (fastest if gcloud is installed)

```bash
# 1. Log in (one browser window, once)
gcloud auth login

# 2. Create a project (id must be globally unique — adjust)
gcloud projects create remy-docs-skill-<yourname> --name="Remy Docs Skill"
gcloud config set project remy-docs-skill-<yourname>

# 3. Enable the two APIs
gcloud services enable docs.googleapis.com drive.googleapis.com

# 4. Create the service account (id must be 6-30 chars)
gcloud iam service-accounts create remy-bot --display-name="Remy 🐀"

# 5. Create the key file where Remy looks for it
mkdir -p ~/.config/remy
gcloud iam service-accounts keys create ~/.config/remy/service_account.json \
  --iam-account=remy-bot@remy-docs-skill-<yourname>.iam.gserviceaccount.com
chmod 600 ~/.config/remy/service_account.json
```

## Path B: Web console (no gcloud needed)

1. Go to https://console.cloud.google.com and create a project
   (e.g. "Remy Docs Skill").
2. **APIs & Services → Library**: enable **Google Docs API** and
   **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create service account**:
   name `remy-bot` (6-30 chars), no roles needed, create.
4. Open the account → **Keys → Add key → Create new key → JSON**.
5. Save the downloaded file as `~/.config/remy/service_account.json`.

## Optional: real suggestions (Developer Preview)

Out of the box Remy can comment. For **real Google Docs suggestions**
(tracked changes with Accept/Reject buttons), the SUGGEST write mode of the
Docs API is currently a Developer Preview feature:

1. Apply at https://developers.google.com/workspace/preview with the project
   number you created above (`gcloud projects describe <id>
   --format="value(projectNumber)"`). Approval took about a day in our case.
2. Once enrolled, add the `preview.py` module to
   `skills/remy/scripts/`. Then
   `remy.py probe <doc>` confirms it. Suggestions then come out as genuine
   Google Docs tracked changes with Accept/Reject buttons, and
   `remy.py suggestions accept|reject` can resolve suggestions — including
   ones your colleagues made.

   The module is **not part of this public build**, because the program terms
   forbid it. Getting it is an internal matter for an enrolled organisation.

**Suggestions work on a comment-only link.** Verified against a document
shared as "anyone with the link can comment" (`canEdit: false`): the
suggestion was created normally. This is the whole point of the feature — a
commenter may suggest in the browser, and with the preview enabled the API
behaves the same. Markup mode, by contrast, is a real edit and does need edit
rights. Before enrollment the identical call fails with *"Write access is
required, comment-only access is not sufficient"*, which is what makes the
preview worth having.

**Why this matters more than it looks.** Verified against the live API in
August 2026: the Docs API *accepts* `writeControl.writeMode = "SUGGEST"` as a
valid enum for non-enrolled projects — no error, no warning — and then applies
the edits **directly to the document** instead of suggesting them. A naive
integration would silently overwrite a colleague's manuscript while reporting
"suggestion created".

Remy therefore never trusts the flag. It probes the real behaviour once (by
inserting and immediately removing a zero-width marker), caches the result in
`~/.config/remy/state.json`, and verifies after every suggestion batch that
suggestions actually appeared. Without preview access Remy writes coloured,
reversible markup where the link allows editing, describes changes in
comments where it does not, and never edits unmarked unless you pass
`--direct`.

### ⚠️ You may not ship preview features publicly

The [Developer Preview Program
terms](https://developers.google.com/workspace/preview) state:

> (ii) program features may not be included in public applications prior to
> the General Availability (GA) announcement.
>
> (iv) I may not grant end users access, outside my domain or company, to
> developer applications that have been built using APIs prior to their GA
> announcement ("Pre-GA APIs") […]

So even once your project is enrolled, suggestion mode may only be used
**inside your own organisation**. That is why this public build does not
contain the code: `skills/remy/scripts/remy.py` has no suggestion-writing
logic in it at all, and setting any environment variable changes nothing.
Enrolled organisations add a `preview.py` module alongside it and keep that
internally.

Markup mode — built entirely on generally available APIs — is the supported
default and needs none of this.

## Verify

```bash
uv run scripts/remy.py whoami
uv run scripts/remy.py check "<your doc share link>"
```

`check` should report `capabilities: {canComment: true}`.

## Sharing with colleagues

Every colleague repeats this setup, so each Remy acts under its own service
account name. Do **not** pass a key file around: it is a bearer secret —
anyone holding it can write to link-shared documents under that robot's
name, and a leak means rotating the key for everyone at once. One person,
one robot account.

## Identity note

Suggestions/comments appear under the service account's name (e.g.
`remy-bot@remy-docs-skill-dirk.iam.gserviceaccount.com`). Anonymous writing —
like the "anonymous animals" in a browser — is not possible through the
official API; some identity is always required.
