"""Body version reconciliation: provenance, paragraph diffs, safe snippets.

Acceptance:
* a word present only in HTML is located against the HTML-derived text;
* a word present in both versions keeps one mail but lists both sources;
* HTML carrying scripts never executes (nor leaks) in snippets or detail;
* legacy rows without originating parts are marked 来源未知, never guessed.
"""
from __future__ import annotations

from app.parser import (
    LABEL_UNKNOWN,
    TextSource,
    match_body_rows,
    parse_eml,
    safe_snippet,
    split_paragraphs,
)
from app.parser.body_reconcile import (
    reconcile_for_detail,
    reconcile_parts,
    tree_from_json,
    version_from_row,
)
from conftest import SAMPLES


def _divergent():
    return parse_eml((SAMPLES / "10_divergent.eml").read_bytes())


def _row(mime_path, ctype, plain_text=None, text=None, source=None):
    return {
        "mime_path": mime_path,
        "content_type": ctype,
        "text_source": source
        or ("html_derived" if ctype == "text/html" else "plain" if ctype == "text/plain" else "unknown"),
        "charset": "utf-8",
        "text": text,
        "plain_text": plain_text,
    }


# ---------- parser-level provenance & grouping ----------

def test_parser_records_text_source_for_each_body():
    p = _divergent()
    sources = {b.mime_path: b.text_source for b in p.bodies}
    assert sources["1"] is TextSource.PLAIN
    assert sources["2"] is TextSource.HTML_DERIVED
    versions = {v.mime_path: v for v in p.body_versions}
    assert versions["1"].known and versions["2"].known
    assert "text/plain" in versions["1"].source_label
    assert "text/html" in versions["2"].source_label


def test_alternatives_grouped_only_by_mime_structure():
    p = _divergent()
    assert len(p.body_diffs) == 1
    diff = p.body_diffs[0]
    assert diff.relation == "alternative"
    assert diff.alternative_path == "0"
    assert {v.mime_path for v in diff.versions} == {"1", "2"}
    assert diff.agreed is False


def test_paragraph_diff_is_explainable():
    diff = _divergent().body_diffs[0]
    assert diff.shared == ["sharedword appears in both versions."]
    only = {note.paragraph: note.present_in for note in diff.unique}
    assert only["This paragraph exists only in the plain version."] == ["1"]
    html_only = "This paragraph exists only in html and carries the htmlonly-token."
    assert only[html_only] == ["2"]
    assert diff.partial == []


def test_standalone_parts_are_not_forced_into_relationship():
    # sample 01: the embedded message/rfc822 body must not join the outer
    # multipart/alternative group.
    p = parse_eml((SAMPLES / "01_multibyte.eml").read_bytes())
    by_key = {d.group_key: d for d in p.body_diffs}
    outer = by_key["1"]  # nearest alternative ancestor of 1.1/1.2/1.3.1
    assert outer.relation == "alternative"
    assert {v.mime_path for v in outer.versions} == {"1.1", "1.2", "1.3.1"}
    # forwarded body stands alone (embedded message resets the lineage)
    embedded = [d for d in p.body_diffs if any(v.mime_path == "2.1" for v in d.versions)]
    assert len(embedded) == 1 and embedded[0].relation == "standalone"


def test_identical_versions_agree():
    parsed = parse_eml(
        b"Message-ID: <same@x>\r\nSubject: s\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nsame paragraph\r\n--B\r\n"
        b"Content-Type: text/html\r\n\r\n<p>same paragraph</p>\r\n--B--\r\n"
    )
    diff = parsed.body_diffs[0]
    assert diff.relation == "alternative"
    assert diff.agreed is True
    assert diff.shared == ["same paragraph"]
    assert diff.unique == []


# ---------- search source + safe snippets ----------

def test_html_only_word_locates_html_derived_version():
    hits = match_body_rows(
        [
            _row("1", "text/plain", text="only plain words here"),
            _row("2", "text/html",
                 plain_text="shared intro and the htmlonly-token appears here"),
        ],
        "htmlonly-token",
    )
    assert len(hits) == 1
    hit = hits[0]
    assert hit.text_source == "html_derived"
    assert hit.mime_path == "2"
    assert hit.content_type == "text/html"
    assert "htmlonly-token" in hit.snippet
    assert hit.snippet.count("<") == 0  # plain text, never markup


def test_shared_word_lists_both_sources_once_each():
    rows = [
        _row("1", "text/plain", text="sharedword in plain"),
        _row("2", "text/html", plain_text="sharedword in html rendering"),
    ]
    hits = match_body_rows(rows, "sharedword")
    assert len(hits) == 2
    assert {h.text_source for h in hits} == {"plain", "html_derived"}
    # repeated occurrences inside one version still return one source entry
    assert len(match_body_rows([_row("1", "text/plain", text="dup dup dup")], "dup")) == 1


def test_script_content_never_matches_or_leaks():
    p = parse_eml((SAMPLES / "10_divergent.eml").read_bytes())
    rows = [
        _row(b.mime_path, b.content_type, plain_text=b.plain_text, text=b.text)
        for b in p.bodies
    ]
    # words living only inside <script> must not be searchable
    assert match_body_rows(rows, "htmlonlyTokenShouldNotLeak") == []
    assert match_body_rows(rows, "document.cookie") == []
    # event-handler tokens are not searchable either
    xss = parse_eml((SAMPLES / "09_html_xss.eml").read_bytes())
    b = xss.bodies[0]
    xss_rows = [_row(b.mime_path, b.content_type, plain_text=b.plain_text, text=b.text)]
    assert match_body_rows(xss_rows, "alert") == []
    assert match_body_rows(xss_rows, "onerror") == []
    # a legitimate word is found and the snippet contains no markup
    hit = match_body_rows(xss_rows, "hello")
    assert len(hit) == 1 and hit[0].text_source == "html_derived"
    assert "<" not in hit[0].snippet and "script" not in hit[0].snippet.lower()


def test_snippet_is_bounded_plain_text():
    text = "word " * 100 + "NEEDLE" + " word" * 100
    s = safe_snippet(text, "needle")
    assert s is not None and "NEEDLE" in s
    assert s.startswith("…") and s.endswith("…")
    assert len(s) <= 204
    # safe_snippet only receives plain text; whitespace is collapsed and the
    # value is returned as a JSON string, so angle brackets carry no semantics
    assert safe_snippet("line one\r\n\tline two NEEDLE end", "needle") == (
        "line one line two NEEDLE end"
    )


# ---------- legacy / unknown provenance ----------

def test_legacy_row_without_originating_part_is_unknown():
    ver = version_from_row(
        {"mime_path": None, "content_type": None, "plain_text": "legacy text", "charset": None}
    )
    assert ver.text_source is TextSource.UNKNOWN
    assert ver.known is False
    assert ver.source_label == LABEL_UNKNOWN


def test_unknown_versions_never_join_alternative_groups():
    rows = [
        _row("1", "text/plain", text="some shared content"),
        _row("2", "text/html", plain_text="some shared content"),
        {"mime_path": None, "content_type": None, "plain_text": "some shared content",
         "charset": None, "text_source": "unknown"},
    ]
    tree = tree_from_json(
        {
            "mime_path": "0", "content_type": "multipart/alternative",
            "disposition": "unknown", "filename": None,
            "is_multipart": True, "is_embedded_message": False, "byte_size": 0,
            "children": [
                {"mime_path": "1", "content_type": "text/plain", "disposition": "unknown",
                 "filename": None, "is_multipart": False, "is_embedded_message": False,
                 "byte_size": 1, "children": []},
                {"mime_path": "2", "content_type": "text/html", "disposition": "unknown",
                 "filename": None, "is_multipart": False, "is_embedded_message": False,
                 "byte_size": 1, "children": []},
            ],
        }
    )
    detail = reconcile_for_detail(rows, tree)
    grouped = {d["group_key"]: d for d in detail["body_diffs"]}
    alt = grouped["0"]
    assert alt["relation"] == "alternative"
    assert {v["mime_path"] for v in alt["versions"]} == {"1", "2"}
    legacy = [d for d in detail["body_diffs"] if any(v["text_source"] == "unknown" for v in d["versions"])]
    assert len(legacy) == 1 and legacy[0]["relation"] == "standalone"
    assert legacy[0]["versions"][0]["source_label"] == LABEL_UNKNOWN


def test_detail_reconciliation_from_parts_roundtrip():
    p = _divergent()
    from dataclasses import asdict

    tree = tree_from_json(asdict(p.tree))
    versions, diffs = reconcile_parts(p.bodies, tree)
    assert [v.mime_path for v in versions] == ["1", "2"]
    assert diffs[0].agreed is False


def test_paragraph_normalization_collapses_html_whitespace():
    assert split_paragraphs("a   b\r\n\nc\t d") == ["a b", "c d"]
