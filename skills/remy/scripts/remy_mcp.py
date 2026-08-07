#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2",
#   "google-api-python-client>=2.100",
#   "google-auth>=2.23",
# ]
# ///
"""Remy as a local MCP server, so Chat and Cowork can reach it too.

Claude Code uses the skill and calls remy.py directly. The Claude desktop app
speaks MCP instead, so this exposes the same operations as tools. It runs as a
local process on your own machine: the service account key never leaves it,
and no server has to be hosted anywhere.

Register it in ~/Library/Application Support/Claude/claude_desktop_config.json:

    {"mcpServers": {"remy": {"command": "<uv>",
                             "args": ["run", "--script", "<this file>"]}}}
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

from mcp.server.mcpserver import MCPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("remy", os.path.join(HERE, "remy.py"))
remy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remy)

mcp = MCPServer("remy", description="Collaborate on Google Docs shared by link.")


class _Args:
    """remy.py's commands take an argparse namespace; this stands in for one."""

    def __init__(self, **kw):
        self.key = None
        self.__dict__.update(kw)


def run(fn, **kw):
    """Call a remy command and capture the JSON it prints on its way out.

    The CLI reports by printing and calling sys.exit, which is right for a
    command line and wrong for a library, so catch both here rather than
    duplicating the logic.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            remy.invoke(fn, _Args(**kw))
    except SystemExit:
        pass
    except Exception as e:
        # A tool call must come back as JSON, never as a dead connection.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
    text = buf.getvalue().strip()
    try:
        return json.loads(text) if text else {"ok": False, "error": "no output"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "unparseable output", "raw": text[:500]}


@mcp.tool()
def session(url: str) -> dict:
    """Start work on a Google Doc: access rights, write mode, language,
    outline, full text, open comments and any @@remy tasks, in one call."""
    return run(remy.cmd_session, doc=url)


@mcp.tool()
def read(url: str, fmt: str = "markdown") -> dict:
    """Read a Google Doc, with collaborators' suggestions applied."""
    return run(remy.cmd_read, doc=url, format=fmt, suggestions="accepted",
               authed=False)


_SUGGEST_DEFAULTS = dict(nth=None, context=None, no_fallback=False,
                         direct=False, markup=None, dry_run=False,
                         force_language=False)


@mcp.tool()
def suggest_replace(url: str, find: str, replace: str,
                    all_occurrences: bool = False) -> dict:
    """Propose replacing text. Lands as a real suggestion where available,
    otherwise as coloured markup. Nothing is ever silently overwritten."""
    return run(remy.cmd_suggest, doc=url, action="replace", find=find,
               replace=replace, all=all_occurrences, **_SUGGEST_DEFAULTS)


@mcp.tool()
def suggest_delete(url: str, find: str, all_occurrences: bool = False) -> dict:
    """Propose deleting text: struck through in pink, never removed outright."""
    return run(remy.cmd_suggest, doc=url, action="delete", find=find,
               all=all_occurrences, **_SUGGEST_DEFAULTS)


def _one_anchor(after, before, at_end):
    """Exactly one of after/before/at_end, or an explanation."""
    if sum([bool(after), bool(before), bool(at_end)]) != 1:
        return {"ok": False, "error": "Give exactly one of after, before "
                                      "or at_end."}
    return None


@mcp.tool()
def suggest_insert(url: str, text: str, after: str = "", before: str = "",
                   at_end: bool = False, heading: int = 0) -> dict:
    """Propose inserting text after/before an anchor, or at the end of the
    document. Exactly one of after, before, at_end must be given. Pass
    heading 1-6 to insert the text as a ready-styled heading paragraph."""
    err = _one_anchor(after, before, at_end)
    if err:
        return err
    return run(remy.cmd_suggest, doc=url, action="insert", text=text,
               after=after or None, before=before or None, end=at_end,
               heading=heading or None, all=False, **_SUGGEST_DEFAULTS)


@mcp.tool()
def suggest_format(url: str, find: str, heading: int = 0,
                   bold: bool = False, italic: bool = False,
                   link: str = "", all_occurrences: bool = False) -> dict:
    """Propose a format change (heading 1-6, bold, italic, or a link): the
    old text is struck through in its old format and the same text is
    re-inserted in the new format, mint-highlighted — resolved with
    markup accept|reject like any other change. For heading, find must
    cover a whole paragraph."""
    return run(remy.cmd_suggest, doc=url, action="format", find=find,
               heading=heading or None, bold=bold, italic=italic,
               link=link or None, all=all_occurrences, **_SUGGEST_DEFAULTS)


@mcp.tool()
def markup(url: str, action: str) -> dict:
    """Resolve Remy's coloured markup: 'list' shows it, 'accept' keeps the
    insertions and removes the struck-through text, 'reject' undoes
    everything Remy proposed."""
    if action not in ("list", "accept", "reject"):
        return {"ok": False, "error": "action must be list, accept or reject"}
    return run(remy.cmd_markup, doc=url, action=action)


@mcp.tool()
def insert_image(url: str, image_url: str, after: str = "", before: str = "",
                 at_end: bool = False, caption: str = "") -> dict:
    """Insert a picture from a publicly reachable URL (Google fetches it).
    Proposed as shaded markup; exactly one of after, before, at_end."""
    err = _one_anchor(after, before, at_end)
    if err:
        return err
    return run(remy.cmd_image, doc=url, url=image_url, after=after or None,
               before=before or None, end=at_end, width=None, height=None,
               caption=caption or None, nth=None, context=None, direct=False)


@mcp.tool()
def comment(url: str, text: str, anchor_text: str = "") -> dict:
    """Leave a comment on a Google Doc."""
    return run(remy.cmd_comment, doc=url, text=text,
               anchor_text=anchor_text or None, quote=None, nth=None,
               context=None)


@mcp.tool()
def reply(url: str, comment_id: str, text: str, resolve: bool = False) -> dict:
    """Reply to a comment; resolve=True also closes the thread."""
    return run(remy.cmd_reply, doc=url, comment_id=comment_id, text=text,
               resolve=resolve)


@mcp.tool()
def task_done(url: str, tag_text: str, result: str = "") -> dict:
    """Close an in-text @@remy task: strikes the tag through (or leaves a
    done-marker comment on a comment-only link) so it never runs twice."""
    return run(remy.cmd_done, doc=url, tag_text=tag_text,
               result=result or None, nth=None, context=None)


@mcp.tool()
def finish(url: str, summary: str = "", open_items: str = "") -> dict:
    """End a working session: reports unresolved markup and returns the exact
    question to put to the user. Pass a summary to sign off in the document."""
    return run(remy.cmd_finish, doc=url, sign_off=bool(summary or open_items),
               summary=summary or None, open_items=open_items or None)


def _run_with_temp_file(fn, content, suffix, **kw):
    """File-taking CLI commands, fed from a parameter.

    The server runs locally, but the agent may live in the cloud — a file
    path would point at the wrong side of the bridge. So the content
    travels as a tool parameter and becomes a temp file only here.
    """
    fh = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8")
    try:
        fh.write(content)
        fh.close()
        return run(fn, file=fh.name, **kw)
    finally:
        os.unlink(fh.name)


@mcp.tool()
def import_markdown(url: str, markdown: str, replace: bool = False,
                    dry_run: bool = False) -> dict:
    """Import Markdown as real document structure: heading styles,
    bold/italic/code, real Docs tables, lists, rules. A direct write into
    an EMPTY document; replace=True overwrites a non-empty one and needs
    the user's explicit consent first."""
    return _run_with_temp_file(remy.cmd_md, markdown, ".md",
                               doc=url, replace=replace, dry_run=dry_run)


@mcp.tool()
def document_runs(url: str) -> dict:
    """Dump every text run (index, text, link, bold, paragraph) — the
    template for rewrite_runs. Group consecutive runs sharing (paragraph,
    link, bold) into translatable units before rewriting; raw runs are
    fragmented by Docs' own edit history."""
    return run(remy.cmd_runs, doc=url)


@mcp.tool()
def rewrite_runs(url: str, changes: dict, user_agreed: bool = False,
                 dry_run: bool = False) -> dict:
    """Replace text runs by index ({index: new text}), keeping character
    styles and links — the way to translate or re-word a document without
    losing its links. Direct, unmarked write: ask the user first, then
    pass user_agreed=True. Batches of ~50 runs per call are fine — runs
    are processed back to front, so partial progress is safe."""
    return _run_with_temp_file(remy.cmd_rewrite, json.dumps(changes),
                               ".json", doc=url, dry_run=dry_run,
                               direct=user_agreed)


@mcp.tool()
def replace_pipe_table(url: str, nth: int = 1, user_agreed: bool = False,
                       dry_run: bool = False) -> dict:
    """Replace the nth Markdown pipe table in the document with a real
    Docs table (bold header, column alignment). Direct, unmarked write:
    ask the user first, then pass user_agreed=True."""
    return run(remy.cmd_table, doc=url, nth=nth, dry_run=dry_run,
               direct=user_agreed)


@mcp.tool()
def whoami() -> dict:
    """Which Google identity Remy is acting as."""
    return run(remy.cmd_whoami)


@mcp.tool()
def setup_guide() -> dict:
    """The Google-account setup, step by step. Call this when Remy has no
    service-account key yet (session/whoami report read-only): walk the user
    through the returned steps in their own language, give them the
    cloudshell_command to paste, then call import_key once they say the key
    file downloaded. This server runs on the user's own computer, so the
    whole setup works from Chat and Cowork too."""
    return run(remy.cmd_setup_guide)


@mcp.tool()
def import_key(file: str = "") -> dict:
    """Find the downloaded remy-key.json (Downloads/Desktop/home), verify it
    against Google, and install it. Pass a path only if the automatic search
    fails. Runs locally on the user's computer."""
    return run(remy.cmd_import_key, file=file or None)


if __name__ == "__main__":
    mcp.run()
