#!/usr/bin/env python3
"""Offline tests for Remy. No network, no credentials, no dependencies.

    python3 skills/remy/tests/test_remy.py

The suite deliberately covers the things that are expensive to get wrong:
the public/preview separation, UTF-16 index arithmetic, anchor
disambiguation, the markup colours, and the @@remy task loop.
"""

import contextlib
import importlib.util
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
REMY_PY = os.path.join(SCRIPTS, "remy.py")
PREVIEW_PY = os.path.join(SCRIPTS, "preview.py")

spec = importlib.util.spec_from_file_location("remy_under_test", REMY_PY)
remy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remy)


@contextlib.contextmanager
def quiet():
    """Swallow the JSON that remy prints on its way out via sys.exit()."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# --------------------------------------------------------------- helpers

def para(*runs, style="NORMAL_TEXT", start=1):
    """Build a Docs API paragraph element from (text, extra_run_fields) pairs."""
    elements, idx = [], start
    for item in runs:
        text, extra = item if isinstance(item, tuple) else (item, {})
        n = remy.u16len(text)
        elements.append({"startIndex": idx, "endIndex": idx + n,
                         "textRun": {"content": text, **extra}})
        idx += n
    return {"startIndex": start, "endIndex": idx,
            "paragraph": {"elements": elements,
                          "paragraphStyle": {"namedStyleType": style}}}, idx


def document(*paragraphs):
    content, idx = [], 1
    for p in paragraphs:
        if isinstance(p, tuple):
            text, style = p
        else:
            text, style = p, "NORMAL_TEXT"
        el, idx = para(text, style=style, start=idx)
        content.append(el)
    return {"title": "T", "revisionId": "rev1", "body": {"content": content}}


def bg(hexcolour):
    r = int(hexcolour[0:2], 16) / 255
    g = int(hexcolour[2:4], 16) / 255
    b = int(hexcolour[4:6], 16) / 255
    return {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}


# --------------------------------------------------------------- the separation

class TestPublicPreviewSeparation(unittest.TestCase):
    """The property that would be embarrassing to break silently."""

    FORBIDDEN = ["writeMode", "acceptSuggestion", "rejectSuggestion",
                 "deleteSuggestion", "writeControl"]

    def test_remy_py_never_contains_preview_api(self):
        with open(REMY_PY, encoding="utf-8") as fh:
            source = fh.read()
        for token in self.FORBIDDEN:
            self.assertNotIn(
                token, source,
                f"{token!r} must live in preview.py, never in remy.py — "
                f"remy.py is the publicly distributed file.")

    def test_without_preview_module_suggestions_are_impossible(self):
        if os.path.exists(PREVIEW_PY):
            self.skipTest("preview.py present — this is the Remy2 build")
        self.assertIsNone(remy._preview)
        self.assertFalse(remy.suggestions_available(None, "doc"))

    def test_preview_module_implements_the_documented_interface(self):
        if not os.path.exists(PREVIEW_PY):
            self.skipTest("public build has no preview module")
        spec2 = importlib.util.spec_from_file_location("preview_ut", PREVIEW_PY)
        preview = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(preview)
        for fn in ("supported", "suggest", "resolve"):
            self.assertTrue(callable(getattr(preview, fn, None)),
                            f"preview.py must expose {fn}()")

    def test_env_var_alone_cannot_enable_suggestions(self):
        if os.path.exists(PREVIEW_PY):
            self.skipTest("preview.py present — this is the Remy2 build")
        os.environ["REMY_ENABLE_PREVIEW_SUGGESTIONS"] = "1"
        try:
            self.assertFalse(remy.suggestions_available(None, "doc"))
        finally:
            os.environ.pop("REMY_ENABLE_PREVIEW_SUGGESTIONS", None)


# --------------------------------------------------------------- parsing

class TestParseDocId(unittest.TestCase):
    def test_accepts_the_url_shapes_people_actually_paste(self):
        wanted = "1tJ9UJ_2AxH7Oow2FSdyff0G5InP6KLbANw4A4zzw2EU"
        for url in [
            f"https://docs.google.com/document/d/{wanted}/edit?usp=sharing",
            f"https://docs.google.com/document/d/{wanted}/edit?tab=t.0#heading=h.x",
            f"https://docs.google.com/document/u/1/d/{wanted}/edit",
            f"https://docs.google.com/document/d/{wanted}",
            wanted,
        ]:
            self.assertEqual(remy.parse_doc_id(url), wanted, url)


class TestParseVersion(unittest.TestCase):
    def test_prerelease_sorts_below_its_final_release(self):
        self.assertLess(remy.parse_version("0.4.0-beta"),
                        remy.parse_version("v0.4.0"))
        self.assertLess(remy.parse_version("v0.4.0"),
                        remy.parse_version("v0.4.1-beta"))

    def test_the_beta_hears_about_its_own_final_release(self):
        """The beta testers must not be the one group the release notice
        skips."""
        final = "v" + remy.VERSION.split("-")[0]
        self.assertGreater(remy.parse_version(final),
                           remy.parse_version(remy.VERSION))


class TestInvoke(unittest.TestCase):
    """API denials must come out as JSON with a hint, not as a traceback.

    The commonest failure of all: a key is configured but the document is
    not shared by link. Matched by class name, so no googleapiclient here."""

    class HttpError(Exception):  # duck-typed like googleapiclient's
        def __init__(self, status):
            super().__init__(f"HTTP {status}")
            self.resp = type("Resp", (), {"status": status})()

    def test_denied_access_fails_with_the_share_hint(self):
        def cmd(args):
            raise self.HttpError(404)
        with quiet() as buf, self.assertRaises(SystemExit) as cm:
            remy.invoke(cmd, None)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("share link", buf.getvalue())

    def test_other_google_errors_fail_as_json_too(self):
        def cmd(args):
            raise self.HttpError(500)
        with quiet() as buf, self.assertRaises(SystemExit) as cm:
            remy.invoke(cmd, None)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Google API request failed", buf.getvalue())

    def test_non_google_errors_still_raise(self):
        def cmd(args):
            raise ValueError("boom")
        with self.assertRaises(ValueError):
            remy.invoke(cmd, None)


class TestU16Len(unittest.TestCase):
    def test_astral_characters_count_as_two(self):
        self.assertEqual(remy.u16len("abc"), 3)
        self.assertEqual(remy.u16len("🐀"), 2)          # the rat is astral
        self.assertEqual(remy.u16len("a🐀b"), 4)
        self.assertEqual(remy.u16len("äöü"), 3)         # BMP: still one each


# --------------------------------------------------------------- indices

class TestDocTextIndices(unittest.TestCase):
    def test_doc_index_matches_the_api_for_plain_text(self):
        doc = document("Hello world\n")
        dt = remy.DocText(doc)
        self.assertEqual(dt.text, "Hello world\n")
        self.assertEqual(dt.doc_index(0), 1)
        self.assertEqual(dt.doc_index(6), 7)

    def test_doc_index_accounts_for_astral_characters(self):
        # "a🐀b": python offsets 0,1,2,3 -> doc indices 1,2,4,5
        doc = document("a🐀b\n")
        dt = remy.DocText(doc)
        self.assertEqual(dt.doc_index(0), 1)
        self.assertEqual(dt.doc_index(1), 2)
        self.assertEqual(dt.doc_index(2), 4, "the rat occupies two UTF-16 units")
        self.assertEqual(dt.doc_index(3), 5)

    def test_text_inside_tables_is_reachable(self):
        cell, end = para("in a cell\n", start=5)
        doc = {"title": "T", "revisionId": "r", "body": {"content": [
            para("before\n", start=1)[0],
            {"startIndex": 4, "endIndex": end, "table": {"tableRows": [
                {"tableCells": [{"content": [cell]}]}]}},
        ]}}
        dt = remy.DocText(doc)
        self.assertIn("in a cell", dt.text)
        hit = dt.find("in a cell")[0]
        self.assertEqual(dt.doc_index(hit[0]), 5)


# --------------------------------------------------------------- anchors

class TestAnchorResolution(unittest.TestCase):
    def setUp(self):
        self.dt = remy.DocText(document("the cat sat on the mat\n"))

    def test_unique_anchor_resolves(self):
        self.assertEqual(len(remy.resolve_anchor(self.dt, "cat")), 1)

    def test_ambiguous_anchor_exits_with_code_2(self):
        with quiet(), self.assertRaises(SystemExit) as cm:
            remy.resolve_anchor(self.dt, "the")
        self.assertEqual(cm.exception.code, 2,
                         "ambiguity must be reported, never guessed")

    def test_nth_and_all_disambiguate(self):
        self.assertEqual(len(remy.resolve_anchor(self.dt, "the", nth=2)), 1)
        self.assertEqual(len(remy.resolve_anchor(self.dt, "the", all_=True)), 2)

    def test_context_narrows_the_search(self):
        hits = remy.resolve_anchor(self.dt, "the", context="the mat")
        self.assertEqual(len(hits), 1)

    def test_missing_anchor_fails_rather_than_guessing(self):
        with quiet(), self.assertRaises(SystemExit) as cm:
            remy.resolve_anchor(self.dt, "elephant")
        self.assertEqual(cm.exception.code, 1)


# --------------------------------------------------------------- language

class TestLanguage(unittest.TestCase):
    EN = ("The results show a clear relationship between the soil type and "
          "the carbon uptake that was measured in this experiment, and it is "
          "the first of the trials to be reported in full with all of the "
          "data that was collected from the field and from the greenhouse.")
    DE = ("Die Ergebnisse zeigen einen klaren Zusammenhang zwischen dem "
          "Bodentyp und der Kohlenstoffaufnahme, die in diesem Versuch "
          "gemessen wurde, und es ist der erste der Versuche, der mit allen "
          "Daten aus dem Feld und aus dem Gewächshaus berichtet wird.")

    def test_detects_english_and_german(self):
        self.assertEqual(remy.detect_language(self.EN)[0], "en")
        self.assertEqual(remy.detect_language(self.DE)[0], "de")

    def test_short_text_is_not_classified(self):
        self.assertEqual(remy.detect_language("Hello there")[0], None)

    def test_enforce_language_blocks_a_mismatch(self):
        dt = remy.DocText(document(self.EN + "\n"))
        with quiet(), self.assertRaises(SystemExit) as cm:
            remy.enforce_language(dt, [{"new": self.DE, "old": ""}])
        self.assertEqual(cm.exception.code, 1)

    def test_enforce_language_allows_a_match(self):
        dt = remy.DocText(document(self.EN + "\n"))
        remy.enforce_language(dt, [{"new": self.EN, "old": ""}])  # no raise

    def test_enforce_language_allows_short_insertions(self):
        dt = remy.DocText(document(self.EN + "\n"))
        remy.enforce_language(dt, [{"new": "M. J. Murphy", "old": ""}])

    def test_force_language_overrides(self):
        dt = remy.DocText(document(self.EN + "\n"))
        remy.enforce_language(dt, [{"new": self.DE, "old": ""}], force=True)


# --------------------------------------------------------------- markup colours

class TestMarkupColours(unittest.TestCase):
    # every swatch in the Google Docs standard colour picker
    PALETTE = """000000 434343 666666 999999 b7b7b7 cccccc d9d9d9 efefef f3f3f3
    ffffff 980000 ff0000 ff9900 ffff00 00ff00 00ffff 4a86e8 0000ff 9900ff
    ff00ff e6b8af f4cccc fce5cd fff2cc d9ead3 d0e0e3 c9daf8 cfe2f3 d9d2e9
    ead1dc dd7e6b ea9999 f9cb9c ffe599 b6d7a8 a2c4c9 a4c2f4 9fc5e8 b4a7d6
    d5a6bd cc4125 e06666 f6b26b ffd966 93c47d 76a5af 6d9eeb 6fa8dc 8e7cc3
    c27ba0 a61c00 cc0000 e69138 f1c232 6aa84f 45818e 3c78d8 3d85c6 674ea7
    a64d79 85200c 990000 b45f06 bf9000 38761d 134f5c 1155cc 0b5394 351c75
    741b47 5b0f00 660000 783f04 7f6000 274e13 0c343d 1c4587 073763 20124d
    4c1130""".split()

    def test_remy_colours_are_absent_from_the_standard_picker(self):
        for name, target in (("insert", remy.MARK_INSERT_BG),
                             ("delete", remy.MARK_DELETE_BG)):
            for swatch in self.PALETTE:
                self.assertFalse(
                    remy.same_colour(bg(swatch), target),
                    f"{name} colour collides with standard swatch #{swatch}; a "
                    f"colleague's own highlighting could be mistaken for Remy's")

    def test_markup_ranges_finds_only_remys_own_marks(self):
        el, _ = para(
            ("kept", {}),
            ("added", {"textStyle": {"backgroundColor": bg("96FADB")}}),
            ("removed", {"textStyle": {"backgroundColor": bg("FD96D5"),
                                       "strikethrough": True}}),
            ("human highlight", {"textStyle": {"backgroundColor": bg("ffff00")}}),
        )
        doc = {"title": "T", "revisionId": "r", "body": {"content": [el]}}
        ins, dele = remy.markup_ranges(doc)
        self.assertEqual(len(ins), 1)
        self.assertEqual(len(dele), 1)

    def test_pink_without_strikethrough_is_not_a_deletion(self):
        el, _ = para(("pink but not struck",
                      {"textStyle": {"backgroundColor": bg("FD96D5")}}))
        doc = {"title": "T", "revisionId": "r", "body": {"content": [el]}}
        self.assertEqual(remy.markup_ranges(doc)[1], [])


# --------------------------------------------------------------- format markup

class TestFormatMarkup(unittest.TestCase):
    """Format changes as markup, Dirk's strategy: the old text is struck
    through in its old format, the same text re-inserted in the new format,
    mint — so reject restores the document exactly with no undo record."""

    def test_heading_needs_a_whole_paragraph(self):
        dt = remy.DocText(document("one two three\n"))
        with quiet(), self.assertRaises(SystemExit) as cm:
            remy.build_format_edit(dt, 4, 7, heading=2)  # just "two"
        self.assertEqual(cm.exception.code, 1)

    def test_heading_refuses_the_documents_last_paragraph(self):
        # the styled copy would land after the body's final newline,
        # where the API allows no insertion
        dt = remy.DocText(document("only line\n"))
        with quiet(), self.assertRaises(SystemExit) as cm:
            remy.build_format_edit(dt, 0, 9, heading=2)
        self.assertEqual(cm.exception.code, 1)

    def test_heading_edit_takes_the_newline_along(self):
        dt = remy.DocText(document("A heading line\n", "after\n"))
        e = remy.build_format_edit(dt, 0, 14, heading=2)
        self.assertEqual(e["doc_range"], (1, 16),
                         "the paragraph's newline must travel with the "
                         "edit, or accept leaves an empty line behind")
        self.assertEqual(e["new"], "A heading line\n")
        self.assertEqual(e["named_style"], "HEADING_2")

    def test_markup_requests_for_a_heading_edit(self):
        e = {"doc_range": (1, 16), "old": "A heading line",
             "new": "A heading line\n", "named_style": "HEADING_2"}
        reqs = remy.markup_requests([e])
        self.assertEqual([next(iter(r)) for r in reqs],
                         ["updateTextStyle", "insertText",
                          "updateParagraphStyle", "updateTextStyle"],
                         "the named style must be applied BEFORE the mint: "
                         "applying it resets the paragraph's text styling "
                         "and would wipe the marking (seen live)")
        self.assertTrue(
            reqs[0]["updateTextStyle"]["textStyle"]["strikethrough"],
            "the original must be struck through, never restyled")
        ps = reqs[2]["updateParagraphStyle"]
        self.assertEqual(ps["paragraphStyle"],
                         {"namedStyleType": "HEADING_2"})
        self.assertEqual((ps["range"]["startIndex"], ps["range"]["endIndex"]),
                         (16, 16 + remy.u16len("A heading line\n")))

    def test_inline_format_copy_carries_the_style(self):
        e = {"doc_range": (5, 8), "old": "two", "new": "two",
             "bold": True, "link": "https://x"}
        reqs = remy.markup_requests([e])
        st = reqs[2]["updateTextStyle"]
        self.assertTrue(st["textStyle"]["bold"])
        self.assertEqual(st["textStyle"]["link"], {"url": "https://x"})
        self.assertIn("bold", st["fields"])
        self.assertIn("link", st["fields"])

    def test_inserted_heading_block_styles_only_its_own_paragraph(self):
        # insert --heading mid-paragraph: the leading newline splits the
        # paragraph and must stay unstyled
        e = {"doc_range": (10, 10), "old": "", "new": "\nTitle\n",
             "named_style": "HEADING_1"}
        reqs = remy.markup_requests([e])
        ps = reqs[1]["updateParagraphStyle"]["range"]
        self.assertEqual((ps["startIndex"], ps["endIndex"]),
                         (11, 10 + remy.u16len("\nTitle\n")))

    def test_direct_format_restyles_in_place(self):
        e = {"doc_range": (1, 16), "old": "A heading line",
             "new": "A heading line\n", "named_style": "HEADING_2"}
        reqs = remy.direct_format_requests([e])
        self.assertEqual([next(iter(r)) for r in reqs],
                         ["updateParagraphStyle"])
        self.assertEqual(reqs[0]["updateParagraphStyle"]["range"],
                         {"startIndex": 1, "endIndex": 16})


# --------------------------------------------------------------- shaded paragraphs

def shaded_para(text, hexcolour, start=1):
    """A paragraph with Remy's picture-markup shading."""
    el, end = para(text, start=start)
    el["paragraph"]["paragraphStyle"]["shading"] = {"backgroundColor": bg(hexcolour)}
    return el, end


class TestShadedParagraphs(unittest.TestCase):
    """How a picture is marked up: its paragraph is shaded, not struck."""

    def build(self, colour):
        p1, e1 = para("before\n", start=1)
        p2, e2 = shaded_para(remy.OBJ_PLACEHOLDER + "\n", colour, start=e1)
        p3, e3 = para("after\n", start=e2)
        doc = {"title": "T", "revisionId": "r",
               "body": {"content": [p1, p2, p3]}}
        return doc, (e1, e2, e3)

    def test_mint_shading_is_an_insertion_with_its_segment_end(self):
        doc, (a, b, seg_end) = self.build("96FADB")
        ins, dele = remy.shaded_paragraphs(doc)
        self.assertEqual(ins, [(a, b, seg_end)])
        self.assertEqual(dele, [])

    def test_pink_shading_is_a_deletion(self):
        doc, (a, b, seg_end) = self.build("FD96D5")
        ins, dele = remy.shaded_paragraphs(doc)
        self.assertEqual(ins, [])
        self.assertEqual(dele, [(a, b, seg_end)])

    def test_a_humans_shading_is_not_remys(self):
        doc, _ = self.build("ffff00")
        self.assertEqual(remy.shaded_paragraphs(doc), ([], []))


class TestImageMarkupRequests(unittest.TestCase):
    """The picture-as-markup batch is pure arithmetic; pin it down."""

    def test_newline_picture_newline_and_both_marks(self):
        image = {"location": {"index": 6}, "uri": "https://x/y.png"}
        reqs = remy.image_markup_requests(6, image)
        self.assertEqual(
            [next(iter(r)) for r in reqs],
            ["insertText", "insertInlineImage", "insertText",
             "updateTextStyle", "updateParagraphStyle"])
        self.assertEqual(reqs[0]["insertText"]["location"]["index"], 6)
        self.assertEqual(reqs[1]["insertInlineImage"]["location"]["index"], 7)
        self.assertEqual(reqs[2]["insertText"]["location"]["index"], 8)
        ts = reqs[3]["updateTextStyle"]["range"]
        self.assertEqual((ts["startIndex"], ts["endIndex"]), (6, 7),
                         "the leading newline must be marked as an insertion")
        ps = reqs[4]["updateParagraphStyle"]["range"]
        self.assertEqual((ps["startIndex"], ps["endIndex"]), (7, 9),
                         "the shaded range must be exactly the picture's "
                         "own paragraph")

    def test_both_marks_use_the_insert_colour(self):
        reqs = remy.image_markup_requests(6, {"location": {}, "uri": "u"})
        ts = reqs[3]["updateTextStyle"]["textStyle"]["backgroundColor"]
        ps = (reqs[4]["updateParagraphStyle"]["paragraphStyle"]
              ["shading"]["backgroundColor"])
        self.assertEqual(ts["color"]["rgbColor"], remy.MARK_INSERT_BG)
        self.assertEqual(ps["color"]["rgbColor"], remy.MARK_INSERT_BG)


class TestMarkupResolutionRequests(unittest.TestCase):
    """Accept/reject arithmetic, including shaded picture paragraphs."""

    def kinds(self, reqs):
        return [next(iter(r)) for r in reqs]

    def rng(self, r):
        body = next(iter(r.values()))
        return (body["range"]["startIndex"], body["range"]["endIndex"])

    def test_reject_removes_a_picture_paragraph_whole(self):
        reqs = remy.markup_resolution_requests(
            "reject", [], [], [(7, 9, 20)], [])
        self.assertEqual(self.kinds(reqs), ["deleteContentRange"])
        self.assertEqual(self.rng(reqs[0]), (7, 9),
                         "the newline must go too, or an empty shaded "
                         "paragraph survives the reject")

    def test_reject_of_the_segments_last_paragraph_unshades_the_rest(self):
        # the segment's final newline cannot be deleted
        reqs = remy.markup_resolution_requests(
            "reject", [], [], [(7, 9, 9)], [])
        self.assertEqual(self.kinds(reqs),
                         ["deleteContentRange", "updateParagraphStyle"])
        self.assertEqual(self.rng(reqs[0]), (7, 8))
        self.assertEqual(self.rng(reqs[1]), (7, 8),
                         "the surviving paragraph must lose its shading")

    def test_reject_of_image_markup_restores_the_document_exactly(self):
        # image_markup_requests(6, ...) leaves a mint newline at (6,7) and
        # the shaded picture paragraph (7,9): reject removes all three
        # characters, picture paragraph first.
        reqs = remy.markup_resolution_requests(
            "reject", [(6, 7, 20)], [], [(7, 9, 20)], [])
        self.assertEqual(self.kinds(reqs),
                         ["deleteContentRange", "deleteContentRange"])
        self.assertEqual([self.rng(r) for r in reqs], [(7, 9), (6, 7)])

    def test_dropping_a_run_at_the_segment_end_spares_the_newline(self):
        # Docs folds the segment's final newline into a marked run that ends
        # the document; deleting it verbatim is an API error (seen live).
        reqs = remy.markup_resolution_requests(
            "accept", [], [(10, 15, 15)], [], [])
        self.assertEqual(self.kinds(reqs),
                         ["deleteContentRange", "updateTextStyle"])
        self.assertEqual(self.rng(reqs[0]), (10, 14),
                         "the final newline must survive the delete")
        self.assertEqual(self.rng(reqs[1]), (10, 11),
                         "the surviving newline must lose its marking")

    def test_accept_of_a_picture_only_clears_the_shading(self):
        reqs = remy.markup_resolution_requests(
            "accept", [], [], [(7, 9, 20)], [])
        self.assertEqual(self.kinds(reqs), ["updateParagraphStyle"])
        body = reqs[0]["updateParagraphStyle"]
        self.assertEqual(body["paragraphStyle"], {"shading": {}})

    def test_accept_drops_struck_text_and_unhighlights_insertions(self):
        reqs = remy.markup_resolution_requests(
            "accept", [(3, 5, 20)], [(10, 14, 20)], [], [])
        self.assertEqual(self.kinds(reqs),
                         ["deleteContentRange", "updateTextStyle"])
        self.assertEqual([self.rng(r) for r in reqs], [(10, 14), (3, 5)],
                         "requests must run in descending index order")


# --------------------------------------------------------------- @@remy tasks

class TestTaskScanning(unittest.TestCase):
    def scan(self, doc, comments=()):
        return remy.collect_tasks(remy.DocText(doc), list(comments), doc)

    def test_finds_a_tag_in_the_text(self):
        found, skipped = self.scan(document("@@remy translate this\n"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["command"], "translate this")
        self.assertEqual(found[0]["source"], "text")

    def test_finds_a_tag_in_a_comment(self):
        found, _ = self.scan(document("body\n"), [
            {"id": "c1", "content": "@@remy fix the intro",
             "author": {"displayName": "A"}}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "comment")

    def test_resolved_comments_are_ignored(self):
        found, _ = self.scan(document("body\n"), [
            {"id": "c1", "content": "@@remy old job", "resolved": True}])
        self.assertEqual(found, [])

    def test_done_marker_comment_suppresses_a_text_tag(self):
        doc = document("@@remy shorten this paragraph by 50% Lorem ipsum\n")
        found, skipped = self.scan(doc, [
            {"id": "c1", "content": remy.DONE_PREFIX + "\ndone",
             "quotedFileContent": {"value": "@@remy shorten this paragraph"}}])
        self.assertEqual(found, [])
        self.assertEqual(skipped, 1)

    def test_struck_through_tag_counts_as_done(self):
        el, _ = para(("@@remy already handled",
                      {"textStyle": {"backgroundColor": bg("FD96D5"),
                                     "strikethrough": True}}))
        doc = {"title": "T", "revisionId": "r", "body": {"content": [el]}}
        found, skipped = self.scan(doc)
        self.assertEqual(found, [])
        self.assertEqual(skipped, 1)

    def test_a_mention_mid_sentence_is_not_an_instruction(self):
        """Documentation about @@remy — and Remy's own reports — must not be
        executed as commands."""
        found, _ = self.scan(document(
            "Anyone can leave a note by starting a line with @@remy fix this.\n"))
        self.assertEqual(found, [], "a mid-sentence mention became a task")

    def test_remys_own_comments_are_not_instructions(self):
        found, _ = self.scan(document("body\n"), [
            {"id": "c1", "content": "Done: the @@remy task was carried out",
             "author": {"displayName": "remy-bot@x.iam.gserviceaccount.com"}}])
        self.assertEqual(found, [], "Remy read its own report as a new task")

    def test_author_me_marks_remys_own_comment(self):
        """Drive's author.me flag is the robust signal — the display name can
        be anything the service account was created with (e.g. 'Remy')."""
        found, _ = self.scan(document("body\n"), [
            {"id": "c1", "content": "@@remy do it again",
             "author": {"displayName": "Remy", "me": True}}])
        self.assertEqual(found, [], "a comment Remy wrote became a task")

    def test_the_loop_terminates(self):
        """A tag must not be reported again once it has been closed."""
        doc = document("@@remy do it\n")
        first, _ = self.scan(doc)
        self.assertEqual(len(first), 1)
        second, skipped = self.scan(doc, [
            {"id": "c1", "content": remy.DONE_PREFIX,
             "quotedFileContent": {"value": first[0]["tag_text"]}}])
        self.assertEqual(second, [], "the same job must not run twice")


# --------------------------------------------------------------- misc

class TestOutline(unittest.TestCase):
    def test_reports_headings_with_levels(self):
        doc = document(("Title", "TITLE"), ("Intro", "HEADING_1"),
                       ("Detail", "HEADING_2"), ("body text", "NORMAL_TEXT"))
        outline = remy.document_outline(doc)
        self.assertEqual([(o["level"], o["text"]) for o in outline],
                         [(0, "Title"), (1, "Intro"), (2, "Detail")])


class TestDocsMatchReality(unittest.TestCase):
    """Documentation drifts silently; these two facts are cheap to pin down."""

    ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
    SKILL_MD = os.path.normpath(os.path.join(HERE, "..", "SKILL.md"))

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_skill_md_documents_every_command(self):
        import re
        source = self.read(REMY_PY)
        # top-level parsers only: `sub.add_parser`, not `ssub`/`msub`/`gsub`
        actual = set(re.findall(r'(?<![a-z])sub\.add_parser\("([a-z-]+)"', source))
        block = self.read(self.SKILL_MD).split("## CLI reference")[1].split("```")[1]
        documented = set()
        for line in block.strip().splitlines():
            if line.startswith("remy.py ") and len(line.split()) > 1:
                documented.update(line.split()[1].split("|"))
        self.assertEqual(actual - documented, set(),
                         "commands exist that SKILL.md does not mention")
        self.assertEqual(documented - actual, set(),
                         "SKILL.md documents commands that no longer exist")

    def test_bundled_mcp_server_is_declared_correctly(self):
        """A wrong path here breaks Cowork silently — nothing errors, the
        server simply never starts."""
        import json
        path = os.path.join(self.ROOT, ".mcp.json")
        if not os.path.exists(path):
            self.skipTest("no bundled MCP server")
        servers = json.loads(self.read(path))["mcpServers"]
        self.assertIn("remy", servers)
        self.assertEqual(servers["remy"]["command"], "/bin/sh",
                         "the launcher must start via /bin/sh — a bare 'uv' "
                         "is not on the desktop app's PATH")
        args = servers["remy"]["args"]
        script = [a for a in args if a.endswith((".sh", ".py"))]
        self.assertEqual(len(script), 1, "expected exactly one script argument")
        resolved = script[0].replace("${CLAUDE_PLUGIN_ROOT}", self.ROOT)
        self.assertTrue(os.path.exists(resolved),
                        f"declared MCP server does not exist: {resolved}")
        if resolved.endswith("remy-mcp-launcher.sh"):
            body = self.read(resolved)
            self.assertIn("remy_mcp.py", body,
                          "the launcher must exec the actual server")
            self.assertTrue(os.path.exists(os.path.join(
                os.path.dirname(resolved), "remy_mcp.py")))

    def test_one_version_everywhere(self):
        """A version that disagrees with itself is the commonest reason a
        plugin submission is rejected."""
        import json
        import re
        found = {"remy.py": remy.VERSION}
        for name in ("plugin.json", "marketplace.json"):
            path = os.path.join(self.ROOT, ".claude-plugin", name)
            if not os.path.exists(path):
                continue
            data = json.loads(self.read(path))
            entry = data if "plugins" not in data else data["plugins"][0]
            if entry.get("version"):
                found[name] = entry["version"]
        changelog = os.path.join(self.ROOT, "CHANGELOG.md")
        if os.path.exists(changelog):
            m = re.search(r"^## (\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)",
                          self.read(changelog), re.M)
            if m:
                found["CHANGELOG.md"] = m.group(1)
        self.assertEqual(len(set(found.values())), 1,
                         f"versions disagree: {found}")

    def test_readme_states_the_actual_test_count(self):
        import re
        n = unittest.defaultTestLoader.loadTestsFromModule(
            sys.modules[type(self).__module__]).countTestCases()
        readme = self.read(os.path.join(self.ROOT, "README.md"))
        m = re.search(r"(\d+) tests", readme)
        self.assertIsNotNone(m, "README no longer states a test count")
        self.assertEqual(int(m.group(1)), n,
                         f"README says {m.group(1)} tests, the suite has {n}")

    def test_docs_name_the_actual_markup_colours(self):
        import re

        def as_hex(colour):
            return "#" + "".join(f"{round(colour[k] * 255):02X}"
                                 for k in ("red", "green", "blue"))

        real = {as_hex(remy.MARK_INSERT_BG), as_hex(remy.MARK_DELETE_BG)}
        for path in (os.path.join(self.ROOT, "README.md"), self.SKILL_MD):
            if not os.path.exists(path):
                continue
            named = {c.upper() for c in
                     re.findall(r"#[0-9A-Fa-f]{6}", self.read(path))}
            self.assertEqual(named - real, set(),
                             f"{os.path.basename(path)} names a colour Remy no "
                             f"longer uses")


class TestPythonWarning(unittest.TestCase):
    """Plain python3 ignores the PEP 723 requires-python header — a field
    test ran on system Python 3.9 without noticing. Old interpreters must
    say so in every reply, without breaking anything."""

    def test_old_python_warns_and_names_the_fix(self):
        w = remy.python_warning((3, 9, 6))
        self.assertIn("3.9", w)
        self.assertIn("uv", w)

    def test_declared_python_stays_silent(self):
        self.assertIsNone(remy.python_warning((3, 10, 0)))
        self.assertIsNone(remy.python_warning((3, 14, 2)))

    def test_out_carries_the_warning(self):
        orig = remy.python_warning
        remy.python_warning = lambda version=None: "TOO OLD"
        try:
            with quiet() as buf, self.assertRaises(SystemExit):
                remy.out({"ok": True})
        finally:
            remy.python_warning = orig
        import json
        self.assertEqual(json.loads(buf.getvalue())["python_warning"],
                         "TOO OLD")


class TestWindowsConsole(unittest.TestCase):
    """A cp1252 Windows console cannot print ⌘ — the success message used
    to crash AFTER the work succeeded, which reads as a failure (found by
    the first Windows field test)."""

    def test_out_survives_a_cp1252_console(self):
        import io
        buf = io.BytesIO()
        cp1252 = io.TextIOWrapper(buf, encoding="cp1252")
        orig = sys.stdout
        sys.stdout = cp1252
        try:
            with self.assertRaises(SystemExit) as cm:
                remy.out({"note": "quit with ⌘Q"})
        finally:
            sys.stdout = orig
        cp1252.flush()
        self.assertEqual(cm.exception.code, 0, "must exit cleanly, not crash")
        self.assertIn(b"\\u2318", buf.getvalue(),
                      "the symbol must come out escaped, not kill the run")


class TestKeyRecognition(unittest.TestCase):
    """Downloads folders contain arbitrary JSON — a top-level list used to
    crash the key finder with AttributeError (found live)."""

    def test_only_a_dict_shaped_key_counts(self):
        self.assertTrue(remy.is_service_account_key(
            {"type": "service_account", "private_key": "x"}))
        for junk in ([1, 2, 3], "text", 7, None, {},
                     {"type": "service_account"},          # no key material
                     {"type": "authorized_user", "private_key": "x"}):
            self.assertFalse(remy.is_service_account_key(junk), repr(junk))

    def test_install_key_rejects_a_list_file_cleanly(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json_path = fh.name
            fh.write("[1, 2, 3]")
        self.addCleanup(os.unlink, json_path)
        with quiet() as buf, self.assertRaises(SystemExit) as cm:
            remy.install_key(json_path)
        self.assertEqual(cm.exception.code, 1,
                         "junk JSON must fail, not crash")
        self.assertIn("not a Google service account key", buf.getvalue())


class TestStableLauncher(unittest.TestCase):
    """install-mcp registers a version-independent launcher, so plugin
    updates reach Chat and Cowork without re-registering."""

    def test_launcher_finds_newest_and_falls_back(self):
        body = remy.stable_launcher_content("/some/checkout/remy_mcp.py")
        self.assertIn("plugins/cache/*/remy/*/skills/remy/scripts/remy_mcp.py",
                      body, "must scan the plugin cache for every version")
        self.assertIn("/some/checkout/remy_mcp.py", body,
                      "must keep the install-time copy as a fallback")
        self.assertIn("-nt", body, "must pick the newest of the candidates")
        self.assertIn("astral.sh/uv/install.sh", body,
                      "missing uv must be explained, not silent")

    def test_launcher_is_valid_shell(self):
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".sh",
                                         delete=False) as fh:
            fh.write(remy.stable_launcher_content("/x/remy_mcp.py"))
            path = fh.name
        self.addCleanup(os.unlink, path)
        p = subprocess.run(["/bin/sh", "-n", path], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_install_mcp_registers_the_stable_path(self):
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            cfg = os.path.join(td, "claude_desktop_config.json")
            args = type("A", (), {"config": cfg, "dry_run": True,
                                  "remove": False, "key": None})()
            with mock.patch("shutil.which", return_value="/usr/bin/uv"), \
                 quiet() as buf, self.assertRaises(SystemExit):
                remy.cmd_install_mcp(args)
            out = json.loads(buf.getvalue())
            entry = out["would_write"]["mcpServers"]["remy"]
            if os.name != "nt":
                self.assertEqual(entry["command"], "/bin/sh")
                self.assertEqual(entry["args"], [remy.STABLE_LAUNCHER])
            self.assertTrue(out["dry_run"])
            self.assertFalse(os.path.exists(cfg), "dry run must write nothing")


class TestGoogleReachability(unittest.TestCase):
    """The cloud-sandbox abort: blocked network fails fast with instructions,
    while any HTTP answer from Google counts as reachable."""

    def swap(self, fake):
        self.orig = remy.urllib.request.urlopen
        remy.urllib.request.urlopen = fake
        self.addCleanup(setattr, remy.urllib.request, "urlopen", self.orig)

    def test_blocked_network_aborts_with_instructions(self):
        def refuse(*a, **k):
            raise remy.urllib.error.URLError(
                "Tunnel connection failed: 403 Forbidden")
        self.swap(refuse)
        with quiet() as buf, self.assertRaises(SystemExit) as cm:
            remy.require_google_reachable()
        self.assertEqual(cm.exception.code, 1)
        out = buf.getvalue()
        self.assertIn("tell_the_user", out)
        self.assertIn("github.com/dirkpaessler/remy", out)

    def test_an_http_answer_from_google_is_not_a_block(self):
        def http_403(*a, **k):
            raise remy.urllib.error.HTTPError(
                "https://docs.google.com", 403, "Forbidden", {}, None)
        self.swap(http_403)
        remy.require_google_reachable()  # no raise: Google answered


class TestAnchorJson(unittest.TestCase):
    def test_build_anchor_shape(self):
        import json
        a = json.loads(remy.build_anchor("rev9", 10, 5))
        self.assertEqual(a["r"], "rev9")
        self.assertEqual(a["a"][0]["txt"], {"o": 9, "l": 5, "ml": 5})


if __name__ == "__main__":
    unittest.main(verbosity=2)
