#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.100",
#   "google-auth>=2.23",
# ]
# ///
"""Remy — the invisible sous-chef for Google Docs.

Deterministic CLI for collaborating on a Google Doc shared by link. Reads
anonymously; with a service account key it proposes changes as reviewable
coloured markup, and reads, writes and resolves comments.

This is the public build. It contains no Developer Preview code, so it cannot
create, accept or reject native Google Docs suggestions — see the preview hook
below.

All output is JSON on stdout. Exit codes: 0 ok, 1 error, 2 ambiguous anchor.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from bisect import bisect_right

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
VERSION = "0.4.1-beta"  # keep in step with .claude-plugin/plugin.json + CHANGELOG
UPDATE_URL = "https://api.github.com/repos/dirkpaessler/remy/tags"
UPDATE_INTERVAL = 24 * 3600  # ask GitHub at most once a day

DEFAULT_KEY_PATH = os.path.expanduser("~/.config/remy/service_account.json")
STATE_PATH = os.path.expanduser("~/.config/remy/state.json")
TAG = "@@remy"
DONE_PREFIX = "✅ @@remy-done"  # marks an in-text tag as handled
# Markup mode: emulate tracked changes with colour, since the API cannot make
# real suggestions. Nothing is ever destroyed — proposed deletions are only
# struck through, so a human decides what actually disappears.
# The two colours are deliberately absent from the Google Docs standard
# palette (max-channel distance 51 and 38 to the nearest standard swatch), so
# highlighting a human applied by hand can never be mistaken for Remy's.
MARK_INSERT_BG = {"red": 0.5882, "green": 0.9804, "blue": 0.8588}  # #96FADB mint
MARK_DELETE_BG = {"red": 0.9922, "green": 0.5882, "blue": 0.8353}  # #FD96D5 pink
COLOUR_TOLERANCE = 0.012  # ~3/255: guards float rounding, nothing else
OBJ_PLACEHOLDER = "￼"  # object replacement char, 1 UTF-16 unit

# ---------------------------------------------------------------- preview hook
# The Docs API can write real tracked-change suggestions, but only for Cloud
# projects enrolled in the Google Workspace Developer Preview — whose terms
# forbid including preview features in publicly distributed applications.
# This build therefore contains none of that code. An enrolled organisation
# can drop a `preview.py` next to this file; see the Remy2 repository.
#
# The module must provide, each taking this module as `host`:
#   supported(host, docs, doc_id, force_recheck=False) -> bool
#   suggest(host, docs, doc_id, requests) -> None          # raises HttpError
#   resolve(host, docs, doc_id, action, ids) -> int        # accept/reject/delete
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import preview as _preview
except ImportError:
    _preview = None


def suggestions_available(docs, doc_id, force_recheck=False):
    """True only if real suggestions are installed AND proven to work."""
    if _preview is None:
        return False
    return bool(_preview.supported(sys.modules[__name__], docs, doc_id,
                                   force_recheck=force_recheck))


def preview_missing(action="that"):
    fail(f"Real Google Docs suggestions are not part of this build, so {action} "
         f"is unavailable.",
         why="Creating, accepting and rejecting suggestions relies on the "
             "Google Workspace Developer Preview, whose terms forbid shipping "
             "preview features publicly.",
         alternative="Markup mode does the same job with generally available "
                     "APIs: mint insertions, pink struck-through deletions, "
                     "resolved with `markup accept|reject`.",
         internal_use="Organisations enrolled in the preview can use the Remy2 "
                      "build, which adds the suggestion module.")


# ---------------------------------------------------------------- utilities

def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def fail(message, **extra):
    out({"ok": False, "error": message, **extra}, code=1)


def invoke(fn, args):
    """Run a command, turning Google API errors into Remy's JSON dialect.

    Any command can hit a document the service account is not allowed to
    see — the commonest failure there is — and that must come out as JSON
    with a hint, never as a traceback. Matched by class name so this file
    keeps working (and stays testable) without googleapiclient installed.
    """
    try:
        fn(args)
    except Exception as err:
        if type(err).__name__ != "HttpError":
            raise
        status = getattr(getattr(err, "resp", None), "status", None)
        if status in (403, 404):
            fail("Google denied Remy's service account access to this "
                 "document.",
                 status=status,
                 hint="Set the document's share link to 'Anyone with the "
                      "link' (Editor for markup, Commenter for comments) "
                      "and re-run. Do not ask the user to invite the "
                      "service account by email.")
        fail("Google API request failed.", status=status,
             detail=str(err)[:300])


def parse_doc_id(url_or_id):
    m = re.search(r"/document/(?:u/\d+/)?d/([a-zA-Z0-9_-]{20,})", url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url_or_id):
        return url_or_id
    fail(f"Could not extract a Google Docs document id from: {url_or_id}")


def u16len(s):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


STOPWORDS = {
    "en": "the and of to in is that for with are was this be by as it from",
    "de": "der die das und von zu den mit ist für auf dem nicht eine sich im",
    "fr": "le la les et de des du une dans pour est que qui par sur au",
    "es": "el la los las de y en que un una por para con del se es",
    "it": "il lo la di che per con del una sono nel alla dei come",
    "nl": "de het een en van is in dat op te met voor zijn niet aan",
    "pt": "o a de que e do da em um para com os as no na",
    "da": "og det at en den til er som på de med for af ikke",
    "sv": "och det att en den till är som på de med för av inte",
}


def detect_language(text):
    """Deterministic stopword-frequency language guess.

    Returns (code, confidence 0..1). Used so Remy writes into a document in
    the language the document is written in, not the language of the chat.
    """
    words = re.findall(r"[a-zA-Zäöüßàâçéèêëîïôùûÿñõáíóúâãêôç]+", text.lower())
    if len(words) < 30:
        return None, 0.0
    counts = {}
    sample = words[:6000]
    for code, sw in STOPWORDS.items():
        hits = set(sw.split())
        counts[code] = sum(1 for w in sample if w in hits) / len(hits)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    best, second = ranked[0], ranked[1]
    if best[1] == 0:
        return None, 0.0
    confidence = 1.0 - (second[1] / best[1])
    return best[0], round(confidence, 3)


# ---------------------------------------------------------------- auth

def key_path(args):
    return args.key or os.environ.get("REMY_KEY") or DEFAULT_KEY_PATH


def parse_version(text):
    """'v1.2.3' -> (1, 2, 3, 1); '1.2.3-beta' -> (1, 2, 3, 0).

    The fourth element ranks a pre-release below its final release, so a
    0.4.0-beta hears about 0.4.0. Unparseable pieces sort as 0.
    """
    text = (text or "").strip()
    nums = [int(p) for p in re.findall(r"\d+", text)[:3]]
    while len(nums) < 3:
        nums.append(0)
    final = 0 if "-" in text.lstrip("v") else 1
    return (*nums, final)


def check_for_update(force=False):
    """Is a newer Remy published? Returns a dict, or None if unknown.

    Deliberately unobtrusive: one request a day at most, cached, silent on
    any failure, and skipped entirely if REMY_NO_UPDATE_CHECK is set. Remy
    never updates itself — it only mentions that a newer version exists.
    """
    if os.environ.get("REMY_NO_UPDATE_CHECK") == "1":
        return None
    import time

    state = load_state()
    cached = state.get("update_check") or {}
    fresh = (time.time() - cached.get("at", 0)) < UPDATE_INTERVAL
    if cached and fresh and not force:
        latest = cached.get("latest")
    else:
        try:
            req = urllib.request.Request(
                UPDATE_URL, headers={"User-Agent": f"remy/{VERSION}",
                                     "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                tags = json.load(resp)
            names = [t.get("name", "") for t in tags if isinstance(t, dict)]
            latest = max(names, key=parse_version) if names else None
        except Exception:
            return None  # offline, rate-limited, repo moved — never a problem
        state["update_check"] = {"at": int(time.time()), "latest": latest}
        save_state(state)

    if not latest:
        return None
    newer = parse_version(latest) > parse_version(VERSION)
    return {
        "installed": VERSION,
        "latest": latest.lstrip("v"),
        "update_available": newer,
        "how": ("Run `claude plugin update remy` (a shell command — you can "
                "run it for the user), or pull the repository if it was "
                "installed by hand." if newer else None),
    }


def require_google_libs():
    """The Google client libraries are declared in the PEP 723 header, so
    `uv run` installs them by itself. Anything else needs them present."""
    try:
        import googleapiclient  # noqa: F401
        import google.oauth2  # noqa: F401
    except ImportError:
        fail("The Google client libraries are not available.",
             run_it_like_this=f"uv run {os.path.abspath(__file__)} ...",
             why="The script declares its own dependencies, so `uv run` "
                 "installs them on first use and nothing has to be set up.",
             without_uv="pip install 'google-api-python-client>=2.100' "
                        "'google-auth>=2.23', then plain python3 works too.")


def load_credentials(args, required=True):
    path = key_path(args)
    if not os.path.exists(path):
        if required:
            fail(
                "No service account key found. Write operations need one.",
                looked_at=path,
                hint="Offer the user the free five-minute setup: run "
                     "`remy.py setup --guide` and walk them through it. "
                     "(Or pass --key / set $REMY_KEY if a key exists "
                     "elsewhere.)",
            )
        return None
    require_google_libs()
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


def services(creds):
    require_google_libs()
    from googleapiclient.discovery import build

    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return docs, drive


# ---------------------------------------------------------------- reading

def require_google_reachable():
    """Fail fast, with instructions, when Google is blocked at the network
    layer — the signature of a claude.ai cloud sandbox (browser Chat/Cowork).

    Any HTTP answer from Google counts as reachable; only a failure to build
    the connection at all (e.g. the sandbox egress proxy refusing the tunnel
    with 403 before TLS starts) triggers the abort. So this never misfires on
    documents that merely deny access."""
    req = urllib.request.Request(
        "https://docs.google.com/robots.txt",
        headers={"User-Agent": f"remy/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=8):
            return
    except urllib.error.HTTPError:
        return  # Google answered — the network path is open
    except Exception as e:
        fail(
            "Google cannot be reached from this environment at all, so Remy "
            "cannot work here — stop, do not retry or debug.",
            detail=str(e)[:200],
            why="claude.ai sessions in a browser (Chat and Cowork) run in a "
                "cloud sandbox whose network allowlist blocks Google. "
                "Installing Remy may have appeared to succeed, but every "
                "Google request is refused before it leaves the sandbox — "
                "and the sandbox is discarded after the session anyway.",
            tell_the_user="Remy cannot run in this window. It works in the "
                          "Claude desktop app on your own computer — in "
                          "Code, Cowork and Chat — and in a local terminal. "
                          "Install it there once; the instructions are in "
                          "the README.",
            instructions="https://github.com/dirkpaessler/remy#getting-started",
        )


def anonymous_export(doc_id, fmt="markdown"):
    fmt_param = {"markdown": "md", "text": "txt", "html": "html"}[fmt]
    url = f"https://docs.google.com/document/d/{doc_id}/export?format={fmt_param}"
    req = urllib.request.Request(url, headers={"User-Agent": "remy-skill/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            final = resp.geturl()
            data = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, str(e.reason)
    if "accounts.google.com" in final:
        return None, "document is not link-readable (redirected to login)"
    text = data.decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    return text, None


# How to view a document that contains other people's suggestions.
#   ACCEPTED — what colleagues see with suggestions applied. Use for READING.
#   INLINE   — raw runs, suggested insertions and deletions both present.
#              The only view whose indices are valid for WRITING.
VIEW_ACCEPTED = "PREVIEW_SUGGESTIONS_ACCEPTED"
VIEW_INLINE = "SUGGESTIONS_INLINE"


def fetch_document(docs, doc_id, view=VIEW_INLINE):
    """Fetch a document.

    Defaults to the inline view because every write path needs indices that
    match the real document. Reading for comprehension should pass
    VIEW_ACCEPTED so Remy sees the text as collaborators see it.
    """
    return docs.documents().get(
        documentId=doc_id, suggestionsViewMode=view).execute()


def collect_segments(content, segs):
    """Flatten structural elements into (doc_start_index, text, flags) segments."""
    for el in content:
        if "paragraph" in el:
            for pe in el["paragraph"].get("elements", []):
                start = pe.get("startIndex")
                if start is None:
                    continue
                tr = pe.get("textRun")
                if tr is not None:
                    segs.append({"doc": start, "text": tr.get("content", "")})
                else:
                    # inline object / page break / footnote ref / equation:
                    # occupies (endIndex - startIndex) UTF-16 units
                    n = pe.get("endIndex", start + 1) - start
                    segs.append({"doc": start, "text": OBJ_PLACEHOLDER * n})
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    collect_segments(cell.get("content", []), segs)
        elif "tableOfContents" in el:
            collect_segments(el["tableOfContents"].get("content", []), segs)
        # sectionBreak: no text content to anchor on


class DocText:
    """Concatenated document text with py-offset -> Docs-index mapping."""

    def __init__(self, document):
        segs = []
        collect_segments(document.get("body", {}).get("content", []), segs)
        self.segs = segs
        self.starts = []  # py start offset per segment
        parts = []
        pos = 0
        for s in segs:
            self.starts.append(pos)
            parts.append(s["text"])
            pos += len(s["text"])
        self.text = "".join(parts)
        self.title = document.get("title", "")
        self.revision = document.get("revisionId", "")

    def doc_index(self, py_offset):
        """Map python string offset to a Docs API (UTF-16) index."""
        i = bisect_right(self.starts, py_offset) - 1
        if i < 0:
            fail(f"Internal error: offset {py_offset} before document start")
        seg = self.segs[i]
        return seg["doc"] + u16len(self.text[self.starts[i]:py_offset])

    def find(self, needle, context=None):
        """Return list of (py_start, py_end) matches; context narrows the search."""
        hay = self.text
        lo, hi = 0, len(hay)
        if context:
            c = hay.find(context)
            if c < 0:
                fail(f"Context text not found in document: {context!r}")
            lo, hi = c, c + len(context)
        matches = []
        start = lo
        while True:
            idx = hay.find(needle, start, hi)
            if idx < 0:
                break
            matches.append((idx, idx + len(needle)))
            start = idx + 1
        return matches

    def snippet(self, py_start, py_end, pad=40):
        a = max(0, py_start - pad)
        b = min(len(self.text), py_end + pad)
        return self.text[a:b].replace("\n", "⏎")


def resolve_anchor(dt, needle, all_=False, nth=None, context=None):
    matches = dt.find(needle, context=context)
    if not matches:
        fail(f"Anchor text not found in document: {needle!r}",
             hint="Use `read` to see the current text; check whitespace/quotes.")
    if all_:
        return matches
    if nth is not None:
        if nth < 1 or nth > len(matches):
            fail(f"--nth {nth} out of range; found {len(matches)} occurrence(s).")
        return [matches[nth - 1]]
    if len(matches) > 1:
        out(
            {
                "ok": False,
                "error": f"Anchor {needle!r} is ambiguous: {len(matches)} occurrences.",
                "occurrences": [
                    {"nth": i + 1, "context": dt.snippet(a, b)}
                    for i, (a, b) in enumerate(matches)
                ],
                "hint": "Re-run with --nth N, --all, or --context to disambiguate.",
            },
            code=2,
        )
    return matches


# ---------------------------------------------------------------- suggesting

def suggestion_ids(document):
    """Map suggestion id -> {inserted: [...], deleted: [...]} of its text.

    Reading these ids is generally available; the runs carry them. Resolving a
    suggestion is a preview-only operation and lives in the preview module.
    """
    found = {}

    def note(sid, kind, text):
        entry = found.setdefault(sid, {"inserted": [], "deleted": []})
        entry[kind].append(text)

    def walk(content):
        for el in content:
            p = el.get("paragraph")
            if p:
                for e in p.get("elements", []):
                    # Text runs carry the ids, but so do inline objects — a
                    # suggested image is invisible if you only look at text.
                    part = e.get("textRun") or e.get("inlineObjectElement")
                    if not part:
                        continue
                    text = part.get("content", "🖼 image")
                    for sid in part.get("suggestedInsertionIds", []) or []:
                        note(sid, "inserted", text)
                    for sid in part.get("suggestedDeletionIds", []) or []:
                        note(sid, "deleted", text)
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))

    walk(document.get("body", {}).get("content", []))
    return found


def count_suggestions(document):
    """Number of text runs carrying a suggested insertion or deletion."""
    n = 0

    def walk(content):
        nonlocal n
        for el in content:
            p = el.get("paragraph")
            if p:
                for e in p.get("elements", []):
                    tr = e.get("textRun") or {}
                    if tr.get("suggestedInsertionIds") or tr.get("suggestedDeletionIds"):
                        n += 1
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))

    walk(document.get("body", {}).get("content", []))
    return n


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def build_anchor(revision_id, doc_start, length):
    """Classic Docs comment anchor: pins a comment to a text range."""
    return json.dumps({
        "r": revision_id,
        "a": [{"txt": {"o": doc_start - 1, "l": length, "ml": length}}],
    })


def comment_fallback(drive, doc_id, body_text, quote=None, anchor=None):
    body = {"content": body_text}
    if quote:
        body["quotedFileContent"] = {"mimeType": "text/plain", "value": quote}
    if anchor:
        body["anchor"] = anchor
    return drive.comments().create(
        fileId=doc_id, body=body, fields="id,content,quotedFileContent,anchor"
    ).execute()


def anchor_for_text(docs, doc_id, anchor_text, nth=None, context=None):
    """Resolve anchor_text in the doc -> (anchor_json, quote_text)."""
    document = fetch_document(docs, doc_id)
    dt = DocText(document)
    (a, b), = resolve_anchor(dt, anchor_text, False, nth, context)
    start = dt.doc_index(a)
    return build_anchor(document.get("revisionId", ""), start,
                        dt.doc_index(b) - start), anchor_text


def iter_runs(document):
    """Yield (startIndex, endIndex, textRun) for every text run in the body."""
    def walk(content):
        for el in content:
            p = el.get("paragraph")
            if p:
                for e in p.get("elements", []):
                    tr = e.get("textRun")
                    if tr and "startIndex" in e:
                        yield e["startIndex"], e["endIndex"], tr
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        yield from walk(cell.get("content", []))
    yield from walk(document.get("body", {}).get("content", []))


def same_colour(style_bg, target):
    rgb = ((style_bg or {}).get("color", {}) or {}).get("rgbColor")
    if not rgb:
        return False
    return all(abs(rgb.get(k, 0) - target[k]) < COLOUR_TOLERANCE
               for k in ("red", "green", "blue"))


def markup_ranges(document):
    """Classify Remy's markup: (insertions, deletions) as index ranges.

    Returns (start, end, segment_end) triples, like shaded_paragraphs():
    when a marked run sits at the very end of its segment, Docs folds the
    segment's final newline into that run, and resolution must know not to
    delete it.
    """
    ins, dele = [], []

    def walk(content):
        seg_end = content[-1]["endIndex"] if content else 0
        for el in content:
            p = el.get("paragraph")
            if p:
                for e in p.get("elements", []):
                    tr = e.get("textRun")
                    if not tr or "startIndex" not in e:
                        continue
                    st = tr.get("textStyle", {})
                    bg = st.get("backgroundColor")
                    if same_colour(bg, MARK_INSERT_BG):
                        ins.append((e["startIndex"], e["endIndex"], seg_end))
                    elif (same_colour(bg, MARK_DELETE_BG)
                          and st.get("strikethrough")):
                        dele.append((e["startIndex"], e["endIndex"], seg_end))
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))

    walk(document.get("body", {}).get("content", []))
    return ins, dele


def shaded_paragraphs(document):
    """Paragraphs Remy shaded — how a picture gets marked up.

    A picture cannot be struck through, so an image proposal is put in its own
    paragraph and the paragraph is shaded instead. Same two colours, same
    meaning.

    Returns (start, end, segment_end) triples: segment_end is the endIndex of
    the last element in the paragraph's segment (body or table cell), because
    a segment's final newline cannot be deleted and resolution must know.
    """
    ins, dele = [], []

    def walk(content):
        seg_end = content[-1]["endIndex"] if content else 0
        for el in content:
            p = el.get("paragraph")
            if p:
                shade = (p.get("paragraphStyle", {}).get("shading", {})
                         .get("backgroundColor"))
                rng = (el["startIndex"], el["endIndex"], seg_end)
                if same_colour(shade, MARK_INSERT_BG):
                    ins.append(rng)
                elif same_colour(shade, MARK_DELETE_BG):
                    dele.append(rng)
            if "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))

    walk(document.get("body", {}).get("content", []))
    return ins, dele


def markup_resolution_requests(action, ins, dele, shaded_ins, shaded_del):
    """Build the batch that accepts or rejects all of Remy's markup.

    Pure function of the ranges, so the arithmetic is testable offline.
    ins/dele and shaded_ins/shaded_del are (start, end, segment_end) triples
    from markup_ranges() and shaded_paragraphs().
    """
    def unshade_req(a, b):
        return {"updateParagraphStyle": {
            "range": {"startIndex": a, "endIndex": b},
            "paragraphStyle": {"shading": {}}, "fields": "shading"}}

    plain = {"backgroundColor": {}, "strikethrough": False}
    requests = []
    if action == "accept":
        # keep insertions (clear their highlight), remove struck-through text
        drop, keep = dele, ins
        unshade, drop_para = shaded_ins, shaded_del
    else:
        # remove insertions, restore struck-through text
        drop, keep = ins, dele
        unshade, drop_para = shaded_del, shaded_ins
    for a, b, _ in sorted(unshade, reverse=True):
        requests.append(unshade_req(a, b))
    for a, b, seg_end in sorted(drop_para, reverse=True):
        if b < seg_end:
            # not the segment's last paragraph: remove it whole, newline
            # included, so neither an empty line nor its shading survives
            requests.append({"deleteContentRange": {
                "range": {"startIndex": a, "endIndex": b}}})
        else:
            # the segment's final newline cannot be deleted: drop the content
            # and clear the shading of the paragraph that remains. The
            # unshade range is (a, a+1) — valid after the delete, which runs
            # first because the sort below is stable for equal indices.
            if b - 1 > a:
                requests.append({"deleteContentRange": {
                    "range": {"startIndex": a, "endIndex": b - 1}}})
            requests.append(unshade_req(a, a + 1))
    for s, t, _ in sorted(keep, reverse=True):
        requests.append({"updateTextStyle": {
            "range": {"startIndex": s, "endIndex": t},
            "textStyle": plain, "fields": "backgroundColor,strikethrough"}})
    for s, t, seg_end in sorted(drop, reverse=True):
        if t == seg_end:
            # Docs folded the segment's final newline into this run, and
            # that newline cannot be deleted: drop the text before it and
            # clear the newline's own marking (delete first — stable sort).
            if t - 1 > s:
                requests.append({"deleteContentRange": {
                    "range": {"startIndex": s, "endIndex": t - 1}}})
            requests.append({"updateTextStyle": {
                "range": {"startIndex": s, "endIndex": s + 1},
                "textStyle": plain,
                "fields": "backgroundColor,strikethrough"}})
        else:
            requests.append({"deleteContentRange": {
                "range": {"startIndex": s, "endIndex": t}}})
    # deletions must run before style updates at lower indices are computed;
    # every list is already descending, so re-sort everything by index desc
    # (stable, so the delete-then-unshade pair above keeps its order)
    def idx(r):
        body = next(iter(r.values()))
        return (body.get("range") or {}).get("startIndex", 0)
    requests.sort(key=idx, reverse=True)
    return requests


def image_markup_requests(index, image):
    """The batch that proposes a picture as markup, deterministically.

    Inserting "\\n" at index ends the current paragraph there; the picture at
    index+1 and the second "\\n" at index+2 then form a paragraph of exactly
    (index+1, index+3), which is shaded mint. The leading newline lands in the
    *preceding* paragraph, so it is marked as a mint text insertion of its
    own — that way `markup reject` removes all three characters and restores
    the document exactly, and `markup accept` merely clears the colours.
    """
    mint = {"color": {"rgbColor": MARK_INSERT_BG}}
    return [
        {"insertText": {"location": {"index": index}, "text": "\n"}},
        {"insertInlineImage": {**image, "location": {"index": index + 1}}},
        {"insertText": {"location": {"index": index + 2}, "text": "\n"}},
        {"updateTextStyle": {
            "range": {"startIndex": index, "endIndex": index + 1},
            "textStyle": {"strikethrough": False, "backgroundColor": mint},
            "fields": "strikethrough,backgroundColor"}},
        {"updateParagraphStyle": {
            "range": {"startIndex": index + 1, "endIndex": index + 3},
            "paragraphStyle": {"shading": {"backgroundColor": mint}},
            "fields": "shading"}},
    ]


def apply_markup(docs, doc_id, edits):
    """Emulate tracked changes with colour. Proposed deletions are struck
    through, never removed; insertions are highlighted."""
    from googleapiclient.errors import HttpError

    requests = []
    for e in sorted(edits, key=lambda e: e["doc_range"][0], reverse=True):
        s, t = e["doc_range"]
        if t > s:  # proposed deletion: strike it, keep the text
            requests.append({"updateTextStyle": {
                "range": {"startIndex": s, "endIndex": t},
                "textStyle": {"strikethrough": True,
                              "backgroundColor": {"color": {"rgbColor": MARK_DELETE_BG}}},
                "fields": "strikethrough,backgroundColor"}})
        if e["new"]:
            requests.append({"insertText": {"location": {"index": t},
                                            "text": e["new"]}})
            requests.append({"updateTextStyle": {
                "range": {"startIndex": t, "endIndex": t + u16len(e["new"])},
                "textStyle": {"strikethrough": False,
                              "backgroundColor": {"color": {"rgbColor": MARK_INSERT_BG}}},
                "fields": "strikethrough,backgroundColor"}})
    try:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}).execute()
    except HttpError as err:
        fail("Markup batchUpdate failed", detail=str(err))
    return {
        "ok": True, "mode": "markup", "edits": len(edits),
        "note": "Changes written into the document as coloured markup: green = "
                "proposed insertion, red + strikethrough = proposed deletion. "
                "Nothing was deleted. Resolve with `remy.py markup accept|reject`, "
                "or clear the highlighting by hand.",
    }


def describe_edit(e):
    if e["new"] and e["old"]:
        return (f"\U0001f400 Remy suggests replacing:\n«{e['old']}»"
                f"\nwith:\n«{e['new']}»")
    if e["new"]:
        return f"\U0001f400 Remy suggests inserting:\n«{e['new']}»"
    return f"\U0001f400 Remy suggests deleting:\n«{e['old']}»"


def post_as_comments(drive, doc_id, edits, note, **extra):
    made = []
    for e in edits:
        c = comment_fallback(drive, doc_id, describe_edit(e), quote=e["old"] or None)
        made.append(c.get("id"))
    return {"ok": True, "mode": "comment-fallback", "edits": len(edits),
            "comment_ids": made, "note": note, **extra}


def enforce_language(dt, edits, force=False):
    """Refuse to insert text written in a different language than the document.

    This is the language rule as an executable invariant rather than an
    instruction: a German summary can no longer end up in an English
    manuscript because someone forgot. Short insertions carry too little
    signal to classify and are always allowed through.
    """
    if force:
        return
    doc_lang, doc_conf = detect_language(dt.text)
    if not doc_lang or doc_conf < 0.15:
        return
    new_text = "\n".join(e["new"] for e in edits if e["new"])
    new_lang, new_conf = detect_language(new_text)
    if not new_lang or new_conf < 0.15 or new_lang == doc_lang:
        return
    fail(
        f"Refusing to write {new_lang!r} text into a {doc_lang!r} document.",
        document_language=doc_lang,
        document_confidence=doc_conf,
        inserted_language=new_lang,
        inserted_confidence=new_conf,
        hint=f"Rewrite the text in '{doc_lang}'. Text Remy puts into a "
             f"document must match the document, not the conversation. "
             f"Use --force-language only if the mismatch is intentional "
             f"(e.g. an explicitly requested translation).",
    )


def can_edit(drive, doc_id):
    try:
        meta = drive.files().get(
            fileId=doc_id, fields="capabilities(canEdit)").execute()
        return bool(meta.get("capabilities", {}).get("canEdit"))
    except Exception:
        return False


def apply_edits(docs, drive, doc_id, edits, fallback=True, direct=False,
                markup=None, dry_run=False):
    """edits: list of {doc_range(start,end), old, new}, any order.

    Every change Remy makes is visible and reversible. Order of preference:
      1. real suggestions, if the probe confirms they work;
      2. coloured markup — the default whenever the document is editable;
      3. a comment describing the change, when Remy may only comment.
    `direct` is the sole escape hatch and requires explicit user consent.
    """
    edits = sorted(edits, key=lambda e: e["doc_range"][0], reverse=True)
    comment_only = markup is False  # the user's explicit --comment-only
    if markup is None and not direct:
        markup = (not suggestions_available(docs, doc_id)
                  and can_edit(drive, doc_id))
    if dry_run:
        mode = ("direct-edit" if direct else "markup" if markup
                else "suggestion" if suggestions_available(docs, doc_id)
                else "comment-fallback")
        return {"ok": True, "mode": mode, "dry_run": True, "edits": len(edits),
                "changes": [{"old": e["old"][:120], "new": e["new"][:120],
                             "range": e["doc_range"]} for e in edits],
                "note": "Nothing was written."}
    if markup:
        return apply_markup(docs, doc_id, edits)
    requests = []
    for e in edits:
        s, t = e["doc_range"]
        if t > s:
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": s, "endIndex": t}}}
            )
        if e["new"]:
            requests.append(
                {"insertText": {"location": {"index": s}, "text": e["new"]}}
            )
    if not requests:
        fail("Nothing to do.")
    from googleapiclient.errors import HttpError

    if direct:
        try:
            docs.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()
        except HttpError as err:
            fail("Direct batchUpdate failed", detail=str(err))
        return {"ok": True, "mode": "direct-edit", "edits": len(edits),
                "note": "Applied directly because --direct was given. "
                        "Undo via File > Version history if unintended."}

    if not suggestions_available(docs, doc_id):
        if not fallback:
            fail("Real suggestions are unavailable and --no-fallback was given.",
                 hint="Enroll the Cloud project in the Workspace Developer "
                      "Preview, or pass --direct to edit the document directly.")
        return post_as_comments(
            drive, doc_id, edits,
            "Changes described as comments because --comment-only was given."
            if comment_only else
            "Remy may only comment on this document — the share link does "
            "not allow editing — so the changes are described as comments. "
            "Markup unlocks when the user sets the link to Editor (Share → "
            "'Anyone with the link' → Editor); do not ask them to invite "
            "the service account by email.")

    before = count_suggestions(fetch_document(docs, doc_id))
    try:
        _preview.suggest(sys.modules[__name__], docs, doc_id, requests)
    except HttpError as err:
        if not fallback:
            fail("Docs API batchUpdate failed", detail=str(err))
        return post_as_comments(drive, doc_id, edits,
                                "batchUpdate failed; proposed as comments.",
                                detail=str(err))
    after_doc = fetch_document(docs, doc_id)
    if count_suggestions(after_doc) <= before:
        # SUGGEST silently degraded to a direct write after all.
        save_state({**load_state(), "suggest_supported": False})
        return {"ok": False, "mode": "direct-edit-unintended", "edits": len(edits),
                "error": "The API accepted SUGGEST but did not create "
                         "suggestions — the edits went into the document "
                         "directly.",
                "action_required": "Undo via File > Version history in the "
                                   "document. Remy has disabled suggestion "
                                   "mode and will use comments from now on."}
    return {"ok": True, "mode": "suggestion", "edits": len(edits)}


# ---------------------------------------------------------------- commands

def cmd_read(args):
    doc_id = parse_doc_id(args.doc)
    # The anonymous export can neither produce raw JSON nor the inline view,
    # and never shows suggestions. Go through the API whenever it must — and
    # for the collaborators' view (the default) whenever a key makes it
    # possible.
    if args.format == "raw" or args.suggestions == "inline":
        args.authed = True
    elif args.suggestions == "accepted" and os.path.exists(key_path(args)):
        args.authed = True
    if not args.authed:
        text, err = anonymous_export(doc_id, args.format)
        if text is not None:
            lang, conf = detect_language(text)
            out({"ok": True, "docId": doc_id, "mode": "anonymous",
                 "format": args.format, "language": lang,
                 "language_confidence": conf, "text": text})
        # fall through to authed read if we have a key
        creds = load_credentials(args, required=False)
        if creds is None:
            fail(f"Anonymous read failed ({err}) and no service account key "
                 f"is configured.")
    creds = load_credentials(args)
    docs, drive = services(creds)
    view = {"accepted": VIEW_ACCEPTED, "inline": VIEW_INLINE}[args.suggestions]
    document = fetch_document(docs, doc_id, view=view)
    if args.format == "raw":
        out({"ok": True, "docId": doc_id, "mode": "service-account",
             "format": "raw", "suggestions": args.suggestions,
             "document": document})
    dt = DocText(document)
    lang, conf = detect_language(dt.text)
    pending = count_suggestions(fetch_document(docs, doc_id, view=VIEW_INLINE))
    out({"ok": True, "docId": doc_id, "mode": "service-account",
         "format": args.format, "suggestions": args.suggestions,
         "pending_suggestions": pending,
         "language": lang, "language_confidence": conf,
         "text": dt.text})


def cmd_suggest(args):
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, drive = services(creds)
    dt = DocText(fetch_document(docs, doc_id))

    edits = []
    if args.action == "replace":
        for a, b in resolve_anchor(dt, args.find, args.all, args.nth, args.context):
            edits.append({
                "doc_range": (dt.doc_index(a), dt.doc_index(b)),
                "old": args.find, "new": args.replace,
            })
    elif args.action == "delete":
        for a, b in resolve_anchor(dt, args.find, args.all, args.nth, args.context):
            edits.append({
                "doc_range": (dt.doc_index(a), dt.doc_index(b)),
                "old": args.find, "new": "",
            })
    elif args.action == "insert":
        if args.all:
            fail("--all is not meaningful for insert.",
                 hint="Insert happens at one place: give a unique anchor "
                      "(--after/--before), or disambiguate with --nth or "
                      "--context.")
        if args.end:
            # insert before the final newline of the body
            idx = dt.doc_index(len(dt.text) - 1) if dt.text else 1
            edits.append({"doc_range": (idx, idx), "old": "", "new": args.text})
        else:
            anchor = args.after or args.before
            (a, b), = resolve_anchor(dt, anchor, False, args.nth, args.context)
            idx = dt.doc_index(b) if args.after else dt.doc_index(a)
            edits.append({"doc_range": (idx, idx), "old": "", "new": args.text})

    enforce_language(dt, edits, force=args.force_language)
    result = apply_edits(docs, drive, doc_id, edits,
                         fallback=not args.no_fallback, direct=args.direct,
                         markup=args.markup, dry_run=args.dry_run)
    result["docId"] = doc_id
    result["title"] = dt.title
    out(result, code=0 if result.get("ok") else 1)


def cmd_markup(args):
    """Resolve Remy's colour markup: accept or reject all of it."""
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, _ = services(creds)
    document = fetch_document(docs, doc_id)
    ins, dele = markup_ranges(document)
    shaded_ins, shaded_del = shaded_paragraphs(document)
    if not ins and not dele and not shaded_ins and not shaded_del:
        out({"ok": True, "docId": doc_id, "insertions": 0, "deletions": 0,
             "note": "No Remy markup found in this document."})

    if args.action == "list":
        def show(ranges):
            items = []
            for s, t, _ in ranges:
                for start, end, tr in iter_runs(document):
                    if start == s:
                        items.append(tr.get("content", "")[:120])
                        break
            return items
        out({"ok": True, "docId": doc_id,
             "insertions": show(ins) + ["🖼 shaded paragraph"] * len(shaded_ins),
             "deletions": show(dele) + ["🖼 shaded paragraph"] * len(shaded_del),
             "note": "Nothing changed. Use `markup accept` or `markup reject`."})

    requests = markup_resolution_requests(args.action, ins, dele,
                                          shaded_ins, shaded_del)

    from googleapiclient.errors import HttpError
    try:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}).execute()
    except HttpError as err:
        fail(f"markup {args.action} failed", detail=str(err))
    out({"ok": True, "docId": doc_id, "action": args.action,
         "insertions": len(ins) + len(shaded_ins),
         "deletions": len(dele) + len(shaded_del),
         "shaded_paragraphs": len(shaded_ins) + len(shaded_del),
         "note": ("Insertions kept, struck-through text removed, highlighting "
                  "cleared." if args.action == "accept" else
                  "Insertions removed, struck-through text restored.")})


def cmd_suggestions(args):
    """List, accept or reject suggestions made by humans in the document."""
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, _ = services(creds)
    document = fetch_document(docs, doc_id)
    found = suggestion_ids(document)

    if args.action == "list":
        out({"ok": True, "docId": doc_id, "count": len(found),
             "suggestions": [
                 {"id": sid,
                  "inserts": "".join(v["inserted"])[:200],
                  "deletes": "".join(v["deleted"])[:200]}
                 for sid, v in found.items()],
             "note": "Nothing changed. Use `suggestions accept|reject` "
                     "with --id, or --all."})

    if _preview is None:
        ing = {"accept": "accepting", "reject": "rejecting",
               "delete": "deleting"}[args.action]
        preview_missing(f"{ing} a suggestion")
    if not found:
        out({"ok": True, "docId": doc_id, "count": 0,
             "note": "No suggestions in this document."})
    ids = args.id or list(found)
    if not args.id and not args.all:
        fail(f"{len(found)} suggestion(s) present. Pass --all to "
             f"{args.action} every one, or --id ID (repeatable).",
             suggestions=[{"id": s, "inserts": "".join(v["inserted"])[:80],
                           "deletes": "".join(v["deleted"])[:80]}
                          for s, v in found.items()])
    unknown = [i for i in ids if i not in found]
    if unknown:
        fail("Unknown suggestion id(s).", unknown=unknown,
             available=list(found))
    n = _preview.resolve(sys.modules[__name__], docs, doc_id, args.action, ids)
    done = args.action + ("d" if args.action.endswith("e") else "ed")
    out({"ok": True, "docId": doc_id, "action": args.action, "resolved": n,
         "note": f"{n} suggestion(s) {done}. This is permanent — "
                 f"the document's version history is the only way back."})


def cmd_image(args):
    """Insert an image from a public URL.

    The Docs API fetches the picture itself, so it needs a URL it can reach —
    Remy cannot upload bytes, and its service account has no Drive of its own
    to host them from. Anything publicly reachable works.
    """
    from googleapiclient.errors import HttpError

    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, drive = services(creds)
    document = fetch_document(docs, doc_id)
    dt = DocText(document)

    if args.end:
        index = document["body"]["content"][-1].get("endIndex", 2) - 1
    else:
        anchor = args.after or args.before
        (a, b), = resolve_anchor(dt, anchor, False, args.nth, args.context)
        index = dt.doc_index(b) if args.after else dt.doc_index(a)

    image = {"location": {"index": index}, "uri": args.url}
    if args.width or args.height:
        size = {}
        if args.width:
            size["width"] = {"magnitude": args.width, "unit": "PT"}
        if args.height:
            size["height"] = {"magnitude": args.height, "unit": "PT"}
        image["objectSize"] = size
    requests = [{"insertInlineImage": image}]

    # A picture cannot be struck through, so markup mode has no equivalent for
    # it. Where suggestions work the human still gets an Accept/Reject button;
    # where they do not, inserting one is an unmarked change and needs saying
    # so out loud rather than happening quietly.
    if suggestions_available(docs, doc_id):
        mode = "suggestion"
        try:
            _preview.suggest(sys.modules[__name__], docs, doc_id, requests)
        except HttpError as err:
            fail("Could not insert the image as a suggestion.",
                 detail=str(err)[:300])
    elif args.direct:
        if not can_edit(drive, doc_id):
            fail("Remy may only comment on this document, and a comment "
                 "cannot carry a picture.",
                 hint="Share the document as Editor, or paste the image in "
                      "yourself.")
        try:
            docs.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}).execute()
        except HttpError as err:
            fail("Could not insert the image.", detail=str(err)[:300],
                 hint="Google fetches the picture from the URL, so it must be "
                      "publicly reachable, under 50 MB and under 25 "
                      "megapixels (PNG, JPEG or GIF).")
        mode = "direct-edit"
    elif can_edit(drive, doc_id):
        # A picture cannot be struck through, so it goes in a paragraph of its
        # own and the paragraph is shaded instead — same two colours, same
        # meaning, and `markup accept|reject` resolves it like any other.
        mode = "markup"
        try:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": image_markup_requests(index, image)},
            ).execute()
        except HttpError as err:
            fail("Could not insert the image.", detail=str(err)[:300],
                 hint="Google fetches the picture from the URL, so it must be "
                      "publicly reachable, under 50 MB and under 25 "
                      "megapixels (PNG, JPEG or GIF).")
    else:
        c = comment_fallback(
            drive, doc_id,
            f"\U0001f400 Remy suggests placing a picture here"
            f"{': ' + args.caption if args.caption else ''}\n{args.url}")
        out({"ok": True, "docId": doc_id, "mode": "comment-fallback",
             "commentId": c.get("id"), "url": args.url,
             "note": "Remy may only comment on this document, and a comment "
                     "cannot carry a picture, so it proposed one in words.",
             "to_insert_it": "share the document as Editor and re-run."})

    caption = None
    if args.caption:
        caption = comment_fallback(
            drive, doc_id,
            f"\U0001f400 Image inserted: {args.caption}\n{args.url}").get("id")
    out({"ok": True, "docId": doc_id, "mode": mode, "url": args.url,
         "caption_comment": caption,
         "note": "Google fetched the picture and stored a copy in the "
                 "document; the source URL is not needed again."})


def cmd_comments(args):
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    _, drive = services(creds)
    out({"ok": True, "docId": doc_id,
         "comments": list_comments(drive, doc_id, args.include_resolved)})


def cmd_comment(args):
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, drive = services(creds)
    anchor, quote = None, args.quote
    if args.anchor_text:
        anchor, quote = anchor_for_text(docs, doc_id, args.anchor_text,
                                        args.nth, args.context)
    c = comment_fallback(drive, doc_id, args.text, quote=quote, anchor=anchor)
    out({"ok": True, "docId": doc_id, "commentId": c.get("id"),
         "quoted": bool(quote),
         "note": "Google Docs ignores anchors set through the API, so the "
                 "comment appears in the document's comment list rather than "
                 "pinned to the text; the quoted text tells readers what it "
                 "refers to. Use markup when something must be visible in "
                 "place."})


def cmd_reply(args):
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    _, drive = services(creds)
    body = {"content": args.text}
    if args.resolve:
        body["action"] = "resolve"
    r = drive.replies().create(
        fileId=doc_id, commentId=args.comment_id, body=body,
        fields="id,action",
    ).execute()
    out({"ok": True, "docId": doc_id, "commentId": args.comment_id,
         "replyId": r.get("id"), "resolved": r.get("action") == "resolve"})


def cmd_tasks(args):
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, drive = services(creds)
    document = fetch_document(docs, doc_id)
    dt = DocText(document)
    comments = list_comments(drive, doc_id, include_resolved=True)
    found, skipped = collect_tasks(dt, comments, document)
    out({"ok": True, "docId": doc_id, "title": dt.title, "tasks": found,
         "already_done": skipped,
         "how_to_complete": {
             "text": "do the work, then `remy.py done <doc> --tag-text "
                     "\"<tag_text>\"` — this strikes the tag through in "
                     "markup, which also stops it being found again",
             "comment": "do the work, then reply --resolve with a result summary",
         }})


def cmd_done(args):
    """Mark an in-text @@remy tag as handled."""
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, drive = services(creds)
    dt = DocText(fetch_document(docs, doc_id))
    (a, b), = resolve_anchor(dt, args.tag_text, False, args.nth, args.context)

    # With edit rights the tag is struck through as markup (or proposed for
    # deletion as a real suggestion) — which is also what stops the scanner
    # finding it again. Only a comment-only link falls back to the ✅ marker.
    if suggestions_available(docs, doc_id) or can_edit(drive, doc_id):
        edits = [{"doc_range": (dt.doc_index(a), dt.doc_index(b)),
                  "old": args.tag_text, "new": ""}]
        result = apply_edits(docs, drive, doc_id, edits, fallback=False)
        result.update(docId=doc_id,
                      marked=("tag struck through as proposed deletion"
                              if result.get("mode") == "markup"
                              else "tag deletion suggested"))
        out(result, code=0 if result.get("ok") else 1)

    anchor, quote = anchor_for_text(docs, doc_id, args.tag_text,
                                    args.nth, args.context)
    text = f"{DONE_PREFIX}\n{args.result}" if args.result else DONE_PREFIX
    c = comment_fallback(drive, doc_id, text, quote=quote, anchor=anchor)
    out({"ok": True, "docId": doc_id, "commentId": c.get("id"),
         "marked": "done-marker comment posted",
         "note": "Remy will skip this tag on future scans. Delete the tag "
                 "text yourself when you are happy with the result."})


def cmd_check(args):
    doc_id = parse_doc_id(args.doc)
    require_google_reachable()
    report = {"ok": True, "docId": doc_id}
    text, err = anonymous_export(doc_id, "text")
    report["anonymous_read"] = True if err is None else f"failed: {err}"
    creds = load_credentials(args, required=False)
    if creds is None:
        report["service_account"] = False
        report["hint"] = "No key configured; read-only mode. See references/SETUP.md"
        out(report)
    docs, drive = services(creds)
    report["service_account"] = creds.service_account_email
    try:
        meta = drive.files().get(
            fileId=doc_id, fields="name,capabilities(canComment,canEdit)"
        ).execute()
        report["title"] = meta.get("name")
        report["capabilities"] = meta.get("capabilities")
    except Exception as e:
        report["drive_access"] = f"failed: {e}"
        out(report, code=1)
    try:
        document = fetch_document(docs, doc_id)
        report["docs_api_read"] = True
        dt = DocText(document)
        lang, conf = detect_language(dt.text)
        report["language"] = lang
        report["language_confidence"] = conf
        report["write_in_language"] = (
            f"Write everything you put INTO this document in '{lang}'."
            if lang else "Language undetermined — match the surrounding text.")
        pending = count_suggestions(document)
        if pending:
            report["pending_suggestions"] = pending
            report["suggestion_warning"] = (
                "This document contains unresolved suggestions from other "
                "people. Remy reads them but cannot accept or reject them — "
                "the Docs API has no request type for that. Text anchors "
                "include suggested-deleted text, so prefer anchors outside "
                "the suggested regions.")
    except Exception as e:
        report["docs_api_read"] = f"failed: {e}"
    if _preview is None:
        report["real_suggestions"] = False
    else:
        state = load_state()
        report["real_suggestions"] = state.get(
            "suggest_supported", "unknown — run `remy.py probe <doc>`")
    if report["real_suggestions"] is True:
        report["write_mode"] = ("suggestion — real tracked changes, which work "
                                "even with comment-only access")
    elif meta.get("capabilities", {}).get("canEdit"):
        report["write_mode"] = ("markup — changes are written as mint "
                                "insertions and pink struck-through "
                                "deletions, resolvable with `markup "
                                "accept|reject`")
    else:
        report["write_mode"] = ("comment — Remy may only comment on this "
                                "document, so changes are described rather "
                                "than written. To enable markup the user "
                                "sets the share link to Editor (Share → "
                                "'Anyone with the link' → Editor); do not "
                                "ask them to invite the service account by "
                                "email.")
    out(report)


def document_outline(document):
    """Heading structure, so the caller does not have to parse it out of text."""
    outline = []
    for el in document.get("body", {}).get("content", []):
        p = el.get("paragraph")
        if not p:
            continue
        style = p.get("paragraphStyle", {}).get("namedStyleType", "")
        if style.startswith("HEADING_") or style == "TITLE":
            text = "".join(e.get("textRun", {}).get("content", "")
                           for e in p.get("elements", [])).strip()
            if text:
                level = 0 if style == "TITLE" else int(style.split("_")[1])
                outline.append({"level": level, "text": text,
                                "startIndex": el["startIndex"]})
    return outline


def collect_tasks(dt, comments, document):
    """The @@remy scan, shared by `tasks` and `session`."""
    done_quotes = [
        (c.get("quotedFileContent") or {}).get("value", "")
        for c in comments if c.get("content", "").startswith(DONE_PREFIX)
    ]
    _, struck = markup_ranges(document)

    def already_done(tag_text, py_start):
        if any(s <= dt.doc_index(py_start) < t for s, t, _ in struck):
            return True
        head = tag_text[:40]
        return any(q and (head in q or q[:40] in tag_text) for q in done_quotes)

    found, skipped = [], 0
    # The tag only counts at the start of a paragraph. Otherwise any sentence
    # that merely mentions @@remy — documentation, or Remy's own report —
    # becomes an instruction, and the skill executes its own examples.
    for m in re.finditer(r"^[ \t>*-]*" + re.escape(TAG) + r"[ :,-]*(.*?)$",
                         dt.text, re.IGNORECASE | re.MULTILINE):
        full = dt.text[m.start():m.end()]
        if already_done(full, m.start()):
            skipped += 1
            continue
        found.append({"id": f"text-{len(found) + 1}", "source": "text",
                      "command": m.group(1).strip(), "tag_text": full,
                      "context": dt.snippet(m.start(), m.end())})
    for c in comments:
        if c.get("resolved") or c.get("content", "").startswith(DONE_PREFIX):
            continue
        # Remy's own reports mention @@remy; they are not new instructions.
        # author.me is "written by the authenticated account" — the robust
        # signal; the displayName suffix stays as a net for older payloads.
        author = c.get("author", {})
        if (author.get("me")
                or author.get("displayName", "")
                .endswith(".iam.gserviceaccount.com")):
            continue
        m = re.search(r"^[ \t>*-]*" + re.escape(TAG) + r"[ :,-]*(.*)",
                      c.get("content", ""),
                      re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if m:
            found.append({"id": c["id"], "source": "comment",
                          "command": m.group(1).strip(),
                          "author": c.get("author", {}).get("displayName"),
                          "quoted": (c.get("quotedFileContent") or {}).get("value")})
    return found, skipped


def list_comments(drive, doc_id, include_resolved=False):
    fields = ("comments(id,content,resolved,anchor,createdTime,"
              "author(displayName,me),quotedFileContent(value),"
              "replies(id,content,author(displayName),action)),nextPageToken")
    result, page_token = [], None
    while True:
        resp = drive.comments().list(
            fileId=doc_id, fields=fields, includeDeleted=False,
            pageSize=100, pageToken=page_token).execute()
        for c in resp.get("comments", []):
            if c.get("resolved") and not include_resolved:
                continue
            result.append(c)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return result


def cmd_session(args):
    """Everything needed to start working on a document, in one call.

    Replaces the check -> read -> tasks sequence so no step can be skipped
    and the caller cannot accidentally read the wrong suggestion view.
    """
    doc_id = parse_doc_id(args.doc)
    require_google_reachable()
    creds = load_credentials(args, required=False)
    if creds is None:
        text, err = anonymous_export(doc_id, "markdown")
        if text is None:
            fail(f"No key configured and anonymous read failed: {err}")
        lang, conf = detect_language(text)
        out({"ok": True, "docId": doc_id, "access": "anonymous-read-only",
             "language": lang, "language_confidence": conf, "text": text,
             "can_write": False,
             "next_steps": [
                 "Remy can read and discuss this document, but writing — "
                 "markup and comments — needs its own Google account, and "
                 "none is set up yet. OFFER THE SETUP PROACTIVELY, in one "
                 "sentence: say what you can and cannot do right now, and "
                 "that a free five-minute browser setup unlocks the rest. "
                 "If the user wants it, run `remy.py setup --guide` and "
                 "walk them through it; if they decline, carry on "
                 "read-only and do not bring it up again.",
             ]})
    docs, drive = services(creds)
    meta = drive.files().get(
        fileId=doc_id, fields="name,capabilities(canComment,canEdit)").execute()
    caps = meta.get("capabilities", {})
    inline = fetch_document(docs, doc_id, view=VIEW_INLINE)
    reading = fetch_document(docs, doc_id, view=VIEW_ACCEPTED)
    dt_read = DocText(reading)
    dt_inline = DocText(inline)
    comments = list_comments(drive, doc_id)
    tasks, skipped = collect_tasks(dt_inline, comments, inline)
    ins, dele = markup_ranges(inline)
    shaded_ins, shaded_del = shaded_paragraphs(inline)
    n_ins, n_del = len(ins) + len(shaded_ins), len(dele) + len(shaded_del)
    lang, conf = detect_language(dt_read.text)
    # Suggestions need no write access — a commenter may suggest, exactly as
    # in the browser. Markup does need it, because it is a real edit.
    suggestions_work = suggestions_available(docs, doc_id)
    mode = ("suggestion" if suggestions_work
            else "markup" if caps.get("canEdit") else "comment")

    steps = []
    if tasks:
        steps.append(f"{len(tasks)} open @@remy task(s) — execute them, then "
                     f"close each one (`done` for in-text tags, "
                     f"`reply --resolve` for comment tags).")
    if n_ins or n_del:
        steps.append(f"Unresolved Remy markup already in the document "
                     f"({n_ins} insertion(s), {n_del} deletion(s)). "
                     f"Ask the user before adding more.")
    steps.append(f"Write everything you put into this document in "
                 f"'{lang}' — enforced, `suggest` refuses other languages.")
    steps.append(f"Changes will be applied as: {mode}."
                 + (" To enable markup the user sets the share link to "
                    "Editor (Share → 'Anyone with the link' → Editor) — "
                    "do not ask them to invite the service account by "
                    "email." if mode == "comment" else ""))
    update = check_for_update()
    if update and update["update_available"]:
        steps.append(f"Remy {update['latest']} is out; you have "
                     f"{update['installed']}. {update['how']} Mention it once, "
                     f"then drop it — it is not urgent.")

    out({
        "ok": True, "docId": doc_id, "title": meta.get("name"),
        "remy_version": VERSION,
        "update": update if (update and update["update_available"]) else None,
        "access": {"canComment": caps.get("canComment"),
                   "canEdit": caps.get("canEdit")},
        "identity": creds.service_account_email,
        "write_mode": mode,
        "language": lang, "language_confidence": conf,
        "pending_suggestions_by_others": count_suggestions(inline),
        "existing_markup": {"insertions": n_ins, "deletions": n_del,
                            "of_which_pictures":
                                len(shaded_ins) + len(shaded_del)},
        "open_comments": [
            {"id": c["id"], "author": c.get("author", {}).get("displayName"),
             "content": c.get("content", "")[:400],
             "quoted": (c.get("quotedFileContent") or {}).get("value", "")[:200]}
            for c in comments],
        "tasks": tasks, "tasks_already_done": skipped,
        "outline": document_outline(reading),
        "next_steps": steps,
        "text": dt_read.text,
    })


def gcloud(*argv, check=True):
    """Run a gcloud command, returning (ok, stdout, stderr).

    Resolved via shutil.which so it also works on Windows, where the
    executable is gcloud.cmd and a bare "gcloud" subprocess call would
    wrongly report the CLI as missing.
    """
    import shutil
    import subprocess
    exe = shutil.which("gcloud")
    if exe is None:
        return False, "", "gcloud-not-found"
    p = subprocess.run((exe,) + argv, capture_output=True, text=True)
    return (p.returncode == 0), p.stdout.strip(), p.stderr.strip()


def is_service_account_key(data):
    """True for a parsed JSON value that is a Google service account key.

    Downloads folders contain arbitrary JSON — including top-level lists,
    on which .get() would crash — so the type check comes first.
    """
    return (isinstance(data, dict)
            and data.get("type") == "service_account"
            and bool(data.get("private_key")))


def find_downloaded_keys():
    """Look for service account key files a user just downloaded."""
    import glob
    candidates = []
    for folder in ("~/Downloads", "~/Desktop", "~", "."):
        for path in glob.glob(os.path.expanduser(folder + "/*.json")):
            try:
                if os.path.getsize(path) > 20000:
                    continue
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if is_service_account_key(data):
                candidates.append((os.path.getmtime(path), path,
                                   data.get("client_email", "")))
    candidates.sort(reverse=True)
    return candidates


def install_key(source):
    """Validate a key file, prove it works, then move it into place."""
    try:
        with open(source, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        fail(f"Could not read {source}: {e}")
    if not is_service_account_key(data):
        fail(f"{source} is not a Google service account key.",
             hint="The file should contain \"type\": \"service_account\".")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    try:
        creds = service_account.Credentials.from_service_account_file(
            source, scopes=SCOPES)
        build("drive", "v3", credentials=creds,
              cache_discovery=False).about().get(fields="user").execute()
    except Exception as e:
        fail("The key file was rejected by Google.", detail=str(e)[:300],
             hint="Make sure the Docs and Drive APIs are enabled for the "
                  "project this key belongs to.")

    os.makedirs(os.path.dirname(DEFAULT_KEY_PATH), exist_ok=True)
    with open(source, encoding="utf-8") as src:
        content = src.read()
    # 0o600 from the first byte — never readable to others, even briefly
    fd = os.open(DEFAULT_KEY_PATH,
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as dst:
        dst.write(content)
    os.chmod(DEFAULT_KEY_PATH, 0o600)  # a pre-existing file keeps its mode
    return data.get("client_email", "")


def cmd_import_key(args):
    """Install a key file the user downloaded, no command line knowledge needed."""
    source = args.file
    found = []
    if not source:
        found = find_downloaded_keys()
        if not found:
            fail("No service account key file found.",
                 looked_in=["~/Downloads", "~/Desktop", "~", "."],
                 hint="Run `remy.py setup --guide` for the setup route, or "
                      "pass the file path with --file.")
        source = found[0][1]
    email = install_key(source)
    out({"ok": True, "imported_from": source, "key": DEFAULT_KEY_PATH,
         "service_account": email,
         "other_candidates": [p for _, p, _ in found[1:4]],
         "verified": "Google accepted the key.",
         "next_steps": ["Remy can now comment on and mark up any Google Doc "
                        "shared with 'anyone with the link'.",
                        "You can delete the downloaded copy of the key file."]})


def cmd_setup_guide(args):
    """The setup route, worded for someone who does not use a terminal.

    Deliberately ONE route: Google Cloud Shell in the browser. It works for
    everyone, installs nothing, costs nothing, and needs no judgement call.
    Developers who already run gcloud can call `remy.py setup` directly;
    passing key files between people is deliberately not offered — one
    person, one robot account.
    """
    out({
        "ok": True,
        "already_configured": os.path.exists(key_path(args)),
        "instruction": "Walk the user through these steps in their own "
                       "language. Do not present a menu of alternatives — "
                       "this is the route. Declining is fine: Remy then "
                       "stays read-only.",
        "steps": [
            "Open https://shell.cloud.google.com and sign in with the "
            "Google account you use for your documents. It opens a black "
            "terminal window inside the browser — you will not have to "
            "understand it.",
            "If asked, agree to activate Cloud Shell.",
            "Paste the setup command Claude gives you and press Enter.",
            "Wait about a minute. A file called remy-key.json downloads "
            "to your computer.",
            "Come back here and say: 'Remy, I downloaded the key file'.",
        ],
        # The tag URL, not main: raw.githubusercontent.com caches per URL
        # for minutes, so right after a release `main` can still serve the
        # previous script. A tag URL is immutable and always matches the
        # version that is asking for it. CI pushes the tag automatically
        # when a version bump lands on main (.github/workflows/
        # tag-release.yml); should it ever be missing, -f makes curl fail
        # cleanly and the one-liner falls back to main.
        "cloudshell_command":
            f"bash <(curl -fsL https://raw.githubusercontent.com/"
            f"dirkpaessler/remy/v{VERSION}/skills/remy/scripts/"
            f"cloudshell-setup.sh || curl -fsL "
            f"https://raw.githubusercontent.com/dirkpaessler/remy/main/"
            f"skills/remy/scripts/cloudshell-setup.sh)",
        "then": "Run `remy.py import-key` — it finds the downloaded file, "
                "verifies it against Google, and installs it.",
        "note": "Nothing is installed on the user's computer and nothing "
                "costs money. If they skip it, Remy reads and discusses "
                "documents but cannot write.",
        "developers": "`remy.py setup` automates the same thing through a "
                      "local gcloud; see references/SETUP.md. Key files are "
                      "secrets — never suggest sharing one between people.",
    })


def cmd_setup(args):
    """Create the user's own Google service account, end to end.

    Everything here is scripted rather than described, so a colleague can get
    write access without reading a setup guide or making a judgement call.
    """
    import hashlib

    steps = []

    ok, _, err = gcloud("--version", check=False)
    if not ok and err == "gcloud-not-found":
        fail("The Google Cloud CLI (gcloud) is not installed.",
             install="macOS: brew install google-cloud-sdk — "
                     "otherwise https://cloud.google.com/sdk/docs/install",
             then="Re-run: remy.py setup")

    ok, account, _ = gcloud("config", "get-value", "account")
    if not ok or not account or account == "(unset)":
        fail("You are not logged in to gcloud.",
             do_this="Run this yourself (it opens a browser once): "
                     "gcloud auth login",
             then="Re-run: remy.py setup")
    steps.append(f"logged in as {account}")

    if os.path.exists(DEFAULT_KEY_PATH) and not args.force:
        out({"ok": True, "already_configured": True,
             "key": DEFAULT_KEY_PATH,
             "note": "A key already exists. Pass --force to create a new "
                     "project and key anyway."})

    project = args.project_id
    if not project and os.path.exists(DEFAULT_KEY_PATH):
        # Reuse the project the current key belongs to. Deriving a fresh name
        # here would strand an existing setup — including, painfully, a
        # project that has been enrolled in the Developer Preview.
        try:
            with open(DEFAULT_KEY_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            project = data.get("project_id") if isinstance(data, dict) else None
        except (OSError, ValueError):
            project = None
        if project:
            steps.append(f"reusing the project your current key belongs to "
                         f"({project}); pass --project-id to override")
    if not project:
        seed = hashlib.sha1(account.encode()).hexdigest()[:6]
        local = re.sub(r"[^a-z0-9]", "", account.split("@")[0].lower())[:12]
        project = f"remy-{local or 'docs'}-{seed}"

    # A deleted project blocks its id for ~30 days: describe still finds
    # it, but every API call fails with RESOURCES_NOT_FOUND. Only an ACTIVE
    # project counts — otherwise walk to a free (or active) sibling id.
    base = project
    for n in range(2, 11):
        ok, state, _ = gcloud("projects", "describe", project,
                              "--format=value(lifecycleState)")
        if not ok:
            ok, _, err = gcloud("projects", "create", project,
                                "--name=Remy Docs Skill")
            if not ok:
                fail(f"Could not create project {project}.", detail=err[:400],
                     hint="Pick your own id with --project-id <id> (must be "
                          "globally unique, 6-30 chars, lowercase).")
            steps.append(f"created project {project}")
            break
        if state == "ACTIVE":
            steps.append(f"project {project} already exists"
                         + ("" if project == base
                            else f" ({base} is pending deletion)"))
            break
        project = f"{base}-{n}"
    else:
        fail(f"No usable project id near {base} — every candidate is "
             f"pending deletion.",
             hint="Pick your own id with --project-id <id>.")
    sa_email = f"remy-bot@{project}.iam.gserviceaccount.com"

    ok, _, err = gcloud("services", "enable", "docs.googleapis.com",
                        "drive.googleapis.com", f"--project={project}")
    if not ok:
        fail("Could not enable the Docs and Drive APIs.", detail=err[:400],
             hint="If this mentions billing or an organisation policy, create "
                  "the project manually per references/SETUP.md.")
    steps.append("enabled Docs + Drive APIs")

    ok, _, err = gcloud("iam", "service-accounts", "describe", sa_email,
                        f"--project={project}")
    if not ok:
        ok, _, err = gcloud("iam", "service-accounts", "create", "remy-bot",
                            "--display-name=Remy", f"--project={project}")
        if not ok:
            fail("Could not create the service account.", detail=err[:400])
        steps.append("created service account remy-bot")
    else:
        steps.append("service account already exists")

    os.makedirs(os.path.dirname(DEFAULT_KEY_PATH), exist_ok=True)
    # A brand-new service account is eventually consistent: creating a key
    # right away can 404. Retry briefly before giving up.
    import time
    for attempt in range(6):
        ok, _, err = gcloud("iam", "service-accounts", "keys", "create",
                            DEFAULT_KEY_PATH, f"--iam-account={sa_email}",
                            f"--project={project}")
        if ok:
            break
        time.sleep(5)
    if not ok:
        fail("Could not create the key file.", detail=err[:400],
             hint="If this mentions iam.disableServiceAccountKeyCreation, "
                  "the Google organisation forbids key files by policy. If "
                  "it mentions a key limit, earlier setups have left keys "
                  "behind (Google allows ten per service account) — delete "
                  "old ones with `gcloud iam service-accounts keys "
                  "list|delete`. Otherwise re-run `remy.py setup` — it is "
                  "idempotent.")
    os.chmod(DEFAULT_KEY_PATH, 0o600)
    steps.append(f"wrote key to {DEFAULT_KEY_PATH}")

    ok, number, _ = gcloud("projects", "describe", project,
                           "--format=value(projectNumber)")
    out({
        "ok": True,
        "project": project,
        "project_number": number if ok else None,
        "service_account": sa_email,
        "key": DEFAULT_KEY_PATH,
        "steps": steps,
        "next_steps": [
            "Remy can now comment on and mark up any Google Doc shared with "
            "'anyone with the link'.",
            "Paste a Google Docs link into the chat to start.",
            "Nothing here costs money: the Docs and Drive APIs are free and "
            "no billing account is attached.",
        ],
        "optional_upgrade": {
            "what": "Real Google Docs suggestions instead of coloured markup — "
                    "they also work on a comment-only link.",
            "how": "Apply to the Google Workspace Developer Preview Program at "
                   "https://developers.google.com/workspace/preview and give "
                   f"it this Cloud project number: {number if ok else '<see `gcloud projects describe`>'}",
            "then": "Add the preview module, which is not part of this "
                    "build, and run `remy.py probe <doc>`.",
            "caveats": [
                "Approval takes a couple of days; it is a best-effort program.",
                "The form asks for a Google Workspace account, so a personal "
                "@gmail.com address may not be accepted.",
                "Enrollment is per Cloud project. The FAQ says service "
                "accounts cannot be added to the program — that refers to "
                "enrolling an account directly; enrolling the project this "
                "service account lives in is what works.",
                "Preview terms forbid passing preview features on to people "
                "outside your own company, so each person enrolls their own "
                "project.",
            ],
        },
    })


def cmd_probe(args):
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, _ = services(creds)
    if _preview is None:
        out({"ok": True, "docId": doc_id, "real_suggestions": False,
             "note": "This build cannot create suggestions — the code is not "
                     "in it. Markup mode is the supported default and needs "
                     "nothing: mint insertions, pink struck-through "
                     "deletions, resolved with `markup accept|reject`.",
             "to_enable": [
                 "Enrol your OWN Cloud project — the one holding the service "
                 "account key — in the Google Workspace Developer Preview at "
                 "https://developers.google.com/workspace/preview. The form "
                 "wants the project NUMBER; `remy.py setup` prints it, or run "
                 "`gcloud projects describe <id> "
                 "--format=\"value(projectNumber)\"`. Approval takes a couple "
                 "of days.",
                 "Then use the Remy2 build, which adds the suggestion module. "
                 "It is for internal use only: the preview terms forbid "
                 "passing preview features to people outside your own "
                 "company.",
             ]})
    supported = suggestions_available(docs, doc_id, force_recheck=True)
    out({
        "ok": True,
        "docId": doc_id,
        "real_suggestions": supported,
        "note": ("Real suggestions work — Remy will use them."
                 if supported else
                 "The suggestion module is installed but this Cloud project "
                 "is not enrolled in the Developer Preview, so Remy falls "
                 "back to markup. Enrol the project at "
                 "https://developers.google.com/workspace/preview."),
        "cached_in": STATE_PATH,
    })


def cmd_finish(args):
    """End a working session.

    Returns the exact question the user must be asked, already worded, so the
    wording lives in code rather than in a prompt that can drift. Optionally
    posts a sign-off comment.
    """
    doc_id = parse_doc_id(args.doc)
    creds = load_credentials(args)
    docs, drive = services(creds)
    document = fetch_document(docs, doc_id)
    title = document.get("title", "this document")
    ins, dele = markup_ranges(document)
    shaded_ins, shaded_del = shaded_paragraphs(document)
    n_ins = len(ins) + len(shaded_ins)
    n_del = len(dele) + len(shaded_del)
    total = n_ins + n_del

    result = {"ok": True, "docId": doc_id, "title": title,
              "markup": {"insertions": n_ins, "deletions": n_del,
                         "of_which_pictures":
                             len(shaded_ins) + len(shaded_del)}}

    if args.sign_off:
        text = "\U0001f400 Remy is done with this session."
        if args.summary:
            text += "\n\nWhat happened:\n" + args.summary
        if args.open_items:
            text += "\n\nStill open:\n" + args.open_items
        result["commentId"] = comment_fallback(drive, doc_id, text).get("id")

    if total == 0:
        result["ask_user"] = None
        result["note"] = "No unresolved markup — nothing to ask about."
        out(result)

    what = []
    if n_ins:
        what.append(f"{n_ins} insertion{'s' if n_ins != 1 else ''}")
    if n_del:
        what.append(f"{n_del} proposed deletion{'s' if n_del != 1 else ''}")
    result["ask_user"] = {
        "instruction": "Ask the user exactly this, in English, using the "
                       "interactive question tool. Do not reword it and do "
                       "not offer shell commands as options.",
        "question": f"Remy marked {' and '.join(what)} in “{title}”. "
                    f"What should happen to them?",
        "header": "Changes",
        "options": [
            {"label": "Keep them for now",
             "description": "The markup stays in the document so you can read "
                            "it in the browser. Just tell me “Remy, accept the "
                            "changes” or “Remy, undo the changes” whenever you "
                            "have decided — no command to remember."},
            {"label": "Accept the changes",
             "description": "Struck-through text is deleted, all highlighting "
                            "is cleared, and the new text stays as normal "
                            "document text."},
            {"label": "Undo the changes",
             "description": "Everything Remy added is removed and the "
                            "struck-through text is restored — the document "
                            "goes back exactly as it was."},
        ],
        "on_answer": {
            "Keep them for now": "do nothing",
            "Accept the changes": f"run: remy.py markup accept {doc_id}",
            "Undo the changes": f"run: remy.py markup reject {doc_id}",
        },
    }
    out(result)


STABLE_LAUNCHER = os.path.expanduser("~/.config/remy/mcp-launcher.sh")


def stable_launcher_content(fallback_server):
    """The launcher install-mcp registers with the desktop app.

    It lives at a version-independent path and finds the NEWEST installed
    remy_mcp.py at start time, so a plugin update reaches Chat and Cowork
    without the connector entry ever being touched again. fallback_server is
    the copy next to the remy.py that ran install-mcp — it keeps checkout
    and hand-copied installations working.
    """
    import shlex
    return f'''#!/bin/sh
# Written by remy.py install-mcp — do not edit; rerunning install-mcp
# rewrites it. Starts the newest installed Remy MCP server.
newest=""
for f in "$HOME"/.claude/plugins/cache/*/remy/*/skills/remy/scripts/remy_mcp.py \\
         {shlex.quote(fallback_server)}; do
  [ -f "$f" ] || continue
  if [ -z "$newest" ] || [ "$f" -nt "$newest" ]; then newest="$f"; fi
done
if [ -z "$newest" ]; then
  echo "remy: no remy_mcp.py found — reinstall the Remy plugin" >&2
  exit 1
fi
for UV in uv "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if command -v "$UV" >/dev/null 2>&1; then
    exec "$UV" run --script "$newest"
  fi
done
echo "remy: uv was not found; install it with:" >&2
echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
exit 1
'''


# The desktop app's connector config lives in a different place per OS.
if sys.platform == "darwin":
    DESKTOP_CONFIG = os.path.expanduser(
        "~/Library/Application Support/Claude/claude_desktop_config.json")
elif os.name == "nt":
    DESKTOP_CONFIG = os.path.join(
        os.environ.get("APPDATA",
                       os.path.expanduser(r"~\AppData\Roaming")),
        "Claude", "claude_desktop_config.json")
else:
    DESKTOP_CONFIG = os.path.expanduser(
        "~/.config/Claude/claude_desktop_config.json")


def cmd_install_mcp(args):
    """Register Remy as a local MCP server so Chat and Cowork can use it.

    Claude Code reaches Remy through the skill. The desktop app's Chat and
    Cowork modes speak MCP instead, so they need this one config entry. The
    server runs locally: the service account key never leaves the machine.
    """
    import shutil

    here = os.path.dirname(os.path.abspath(__file__))
    server = os.path.join(here, "remy_mcp.py")
    launcher = os.path.join(here, "remy-mcp-launcher.sh")
    if not os.path.exists(server):
        fail("remy_mcp.py is missing next to remy.py.", looked_at=server)
    uv = shutil.which("uv")
    if uv is None:
        fallback = os.path.expanduser(
            r"~\.local\bin\uv.exe" if os.name == "nt" else "~/.local/bin/uv")
        uv = fallback if os.path.exists(fallback) else None
    if uv is None:
        fail("uv was not found, and the MCP server needs it to run.",
             install=('powershell -ExecutionPolicy ByPass -c '
                      '"irm https://astral.sh/uv/install.ps1 | iex"'
                      if os.name == "nt" else
                      "curl -LsSf https://astral.sh/uv/install.sh | sh"))

    # Register the stable launcher: it lives at a version-independent path
    # and picks the newest installed server at every app start, so both uv
    # moving and plugin updates are survived without re-registering. On
    # Windows (no /bin/sh) the resolved absolute uv path is used directly.
    if os.name != "nt":
        entry = {"command": "/bin/sh", "args": [STABLE_LAUNCHER]}
    else:
        entry = {"command": uv, "args": ["run", "--script", server]}
    config = {}
    if os.path.exists(args.config):
        try:
            with open(args.config, encoding="utf-8") as fh:
                config = json.load(fh)
        except ValueError as e:
            fail(f"{args.config} is not valid JSON, so Remy will not touch it.",
                 detail=str(e))

    existing = (config.get("mcpServers") or {}).get("remy")
    if args.remove:
        if not existing:
            out({"ok": True, "note": "Remy was not registered; nothing to do."})
        del config["mcpServers"]["remy"]
    else:
        config.setdefault("mcpServers", {})["remy"] = entry

    if args.dry_run:
        out({"ok": True, "dry_run": True, "config_file": args.config,
             "would_write": {"mcpServers": config.get("mcpServers", {})},
             "already_registered": existing == entry,
             "note": "Nothing was written."})

    if not args.remove and os.name != "nt":
        os.makedirs(os.path.dirname(STABLE_LAUNCHER), exist_ok=True)
        with open(STABLE_LAUNCHER, "w", encoding="utf-8") as fh:
            fh.write(stable_launcher_content(server))
        os.chmod(STABLE_LAUNCHER, 0o755)

    # The backup is the state before Remy's FIRST intervention; a later run
    # must not replace it with a config Remy already touched.
    backup = args.config + ".remy-backup"
    if os.path.exists(args.config) and not os.path.exists(backup):
        shutil.copy2(args.config, backup)
    os.makedirs(os.path.dirname(args.config), exist_ok=True)
    with open(args.config, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    out({"ok": True,
         "action": "removed" if args.remove else "registered",
         "config_file": args.config,
         "backup": backup if os.path.exists(backup) else None,
         "server": entry,
         "next_steps": [
             "Quit the Claude app completely (⌘Q) and open it again — it reads "
             "this file only at startup.",
             "Remy's tools then appear in Chat and in Cowork. Claude Code is "
             "unaffected; it keeps using the skill.",
             "The entry survives plugin updates: the registered launcher "
             "starts whatever the newest installed Remy is.",
             "Undo any time with `remy.py install-mcp --remove`.",
         ]})


def cmd_version(args):
    info = check_for_update(force=True)
    if info is None:
        out({"ok": True, "installed": VERSION,
             "latest": "could not reach GitHub — no matter, Remy works offline",
             "update_available": None})
    out({"ok": True, **info})


def cmd_whoami(args):
    creds = load_credentials(args, required=False)
    if creds is None:
        out({"ok": True, "identity": "anonymous (read-only)",
             "key_path_checked": key_path(args)})
    out({"ok": True, "identity": creds.service_account_email,
         "key": key_path(args),
         "note": "This robot account needs no invitation: it can work on "
                 "any Google Doc shared with 'anyone with the link', as far "
                 "as that link allows. Its writes are attributed to the "
                 "robot, never to the user."})


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="remy", description=__doc__)
    p.add_argument("--key", help="path to service account JSON key")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("read", help="read the document")
    s.add_argument("doc")
    s.add_argument("--format", choices=["markdown", "text", "raw"],
                   default="markdown")
    s.add_argument("--suggestions", choices=["accepted", "inline"],
                   default="accepted",
                   help="'accepted' (default) shows the document as "
                        "collaborators see it, with pending suggestions "
                        "applied; 'inline' shows the raw runs")
    s.add_argument("--authed", action="store_true",
                   help="skip anonymous export, use the service account")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("suggest", help="propose an edit (suggestion or fallback comment)")
    ssub = s.add_subparsers(dest="action", required=True)

    r = ssub.add_parser("replace")
    r.add_argument("doc")
    r.add_argument("--find", required=True)
    r.add_argument("--replace", required=True)
    d = ssub.add_parser("delete")
    d.add_argument("doc")
    d.add_argument("--find", required=True)
    i = ssub.add_parser("insert")
    i.add_argument("doc")
    i.add_argument("--text", required=True)
    g = i.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="anchor text; insert right after it")
    g.add_argument("--before", help="anchor text; insert right before it")
    g.add_argument("--end", action="store_true", help="append at end of body")
    for sp in (r, d, i):
        sp.add_argument("--all", action="store_true",
                        help="apply to every occurrence")
        sp.add_argument("--nth", type=int,
                        help="apply to the Nth occurrence (1-based)")
        sp.add_argument("--context",
                        help="only search inside this larger text span")
        sp.add_argument("--no-fallback", action="store_true",
                        help="fail instead of falling back to a comment")
        sp.add_argument("--force-language", action="store_true",
                        help="allow inserting text in a language other than "
                             "the document's (e.g. a requested translation)")
        sp.add_argument("--dry-run", action="store_true",
                        help="show what would change and in which mode, "
                             "without writing anything")
        sp.add_argument("--comment-only", dest="markup", action="store_false",
                        default=None,
                        help="describe the change in a comment instead of "
                             "writing coloured markup into the document")
        sp.add_argument("--direct", action="store_true",
                        help="EDIT THE DOCUMENT DIRECTLY without any marking; "
                             "only with explicit user consent")
        sp.add_argument("--key")
        sp.set_defaults(func=cmd_suggest)

    s = sub.add_parser("markup", help="accept or reject Remy's colour markup")
    msub = s.add_subparsers(dest="action", required=True)
    for act in ("list", "accept", "reject"):
        m = msub.add_parser(act)
        m.add_argument("doc")
        m.add_argument("--key")
        m.set_defaults(func=cmd_markup)

    s = sub.add_parser("suggestions",
                       help="list, accept or reject humans' suggestions")
    gsub = s.add_subparsers(dest="action", required=True)
    for act in ("list", "accept", "reject", "delete"):
        g = gsub.add_parser(act)
        g.add_argument("doc")
        g.add_argument("--id", action="append",
                       help="suggestion id (repeatable)")
        g.add_argument("--all", action="store_true",
                       help="apply to every suggestion in the document")
        g.add_argument("--key")
        g.set_defaults(func=cmd_suggestions)

    s = sub.add_parser("image", help="insert a picture from a public URL")
    s.add_argument("doc")
    s.add_argument("--url", required=True,
                   help="publicly reachable image URL; Google fetches it")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="anchor text; place it after")
    g.add_argument("--before", help="anchor text; place it before")
    g.add_argument("--end", action="store_true", help="at the end of the body")
    s.add_argument("--width", type=float, help="width in points")
    s.add_argument("--height", type=float, help="height in points")
    s.add_argument("--caption", help="also leave a comment saying what it is")
    s.add_argument("--nth", type=int)
    s.add_argument("--context")
    s.add_argument("--direct", action="store_true")
    s.set_defaults(func=cmd_image)

    s = sub.add_parser("comments", help="list comments")
    s.add_argument("doc")
    s.add_argument("--include-resolved", action="store_true")
    s.set_defaults(func=cmd_comments)

    s = sub.add_parser("comment", help="create a comment")
    s.add_argument("doc")
    s.add_argument("--text", required=True)
    s.add_argument("--anchor-text",
                   help="doc text to pin this comment to (preferred)")
    s.add_argument("--quote", help="quoted text shown, when not using --anchor-text")
    s.add_argument("--nth", type=int, help="disambiguate --anchor-text")
    s.add_argument("--context", help="disambiguate --anchor-text")
    s.set_defaults(func=cmd_comment)

    s = sub.add_parser("reply", help="reply to a comment (optionally resolve)")
    s.add_argument("doc")
    s.add_argument("--comment-id", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--resolve", action="store_true")
    s.set_defaults(func=cmd_reply)

    s = sub.add_parser("tasks", help=f"scan text + comments for {TAG} commands")
    s.add_argument("doc")
    s.set_defaults(func=cmd_tasks)

    s = sub.add_parser("finish", help="end the session; returns the exact "
                                      "question to ask the user")
    s.add_argument("doc")
    s.add_argument("--sign-off", action="store_true",
                   help="also post a summary comment in the document")
    s.add_argument("--summary", help="what Remy did in this session")
    s.add_argument("--open-items", help="anything left open for the humans")
    s.set_defaults(func=cmd_finish)

    s = sub.add_parser("session", help="start work: access, language, tasks, "
                                       "outline and text in one call")
    s.add_argument("doc")
    s.set_defaults(func=cmd_session)

    s = sub.add_parser("setup", help="create your own Google service account "
                                     "so Remy can write")
    s.add_argument("--project-id", help="Google Cloud project id to use")
    s.add_argument("--force", action="store_true",
                   help="set up again even if a key already exists")
    s.add_argument("--guide", action="store_true",
                   help="show the setup options in plain language instead of "
                        "running anything")
    s.set_defaults(func=lambda a: cmd_setup_guide(a) if a.guide else cmd_setup(a))

    s = sub.add_parser("import-key", help="install a key file the user "
                                          "downloaded (finds it automatically)")
    s.add_argument("--file", help="path to the key file; searched for if omitted")
    s.set_defaults(func=cmd_import_key)

    s = sub.add_parser("check", help="probe access level for a document")
    s.add_argument("doc")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("done", help="mark an in-text @@remy tag as handled")
    s.add_argument("doc")
    s.add_argument("--tag-text", required=True,
                   help="the full tag_text reported by `tasks`")
    s.add_argument("--result", help="one-line summary of what was done")
    s.add_argument("--nth", type=int)
    s.add_argument("--context")
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("probe", help="test whether real suggestions work "
                                     "(writes and removes a marker)")
    s.add_argument("doc")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("install-mcp",
                       help="register Remy with the Claude desktop app so "
                            "Chat and Cowork can use it")
    s.add_argument("--config", default=DESKTOP_CONFIG,
                   help="path to claude_desktop_config.json")
    s.add_argument("--dry-run", action="store_true",
                   help="show what would be written, change nothing")
    s.add_argument("--remove", action="store_true", help="unregister again")
    s.set_defaults(func=cmd_install_mcp)

    s = sub.add_parser("version", help="installed version, and whether a "
                                       "newer one has been published")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("whoami", help="show the identity Remy acts as")
    s.set_defaults(func=cmd_whoami)

    args = p.parse_args()
    invoke(args.func, args)


if __name__ == "__main__":
    main()
