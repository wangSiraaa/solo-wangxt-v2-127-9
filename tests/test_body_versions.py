"""Body-version reconciliation: provenance, paragraph diffs, safe snippets.

Covers the acceptance contract:

* a word present only in the HTML alternative is located on the
  **html-derived** version (with its MIME path);
* a word present in both versions keeps a single search result but lists both
  sources (plain part and html-derived);
* script-bearing HTML never executes/leaks into snippets, diffs or detail
  views — search works over extracted text only;
* legacy records whose original parts cannot be obtained are marked
  ``unknown`` ("来源未知"), never paired or diffed by guesswork.
"""
from __future__ import annotations

from app.body_compare import compare_versions, split_paragraphs
from app.body_facts import body_matches, make_snippet
from app.parser import AltRelation, TextSource, parse_eml
from conftest import SAMPLES


def _body_rows(parsed):
    return [
        {
            "mime_path": b.mime_path,
            "content_type": b.content_type,
            "text_source": b.text_source.value,
            "alt_relation": b.alt_relation.value,
            "plain_text": b.plain_text,
            "text": b.text,
        }
        for b in parsed.bodies
    ]


# ---------- parser provenance / grouping -----------------------------------


def test_alternative_versions_grouped_with_mime_paths_and_sources():
    p = parse_eml((SAMPLES / "10_alt_divergent.eml").read_bytes())
    assert p.status.value == "ok"
    paths = {b.mime_path: b for b in p.bodies}
    assert set(paths) == {"1", "2"}
    assert paths["1"].text_source is TextSource.PLAIN_PART
    assert paths["1"].alt_relation is AltRelation.PLAIN
    assert paths["2"].text_source is TextSource.HTML_DERIVED
    assert paths["2"].alt_relation is AltRelation.HTML
    assert paths["1"].alternative_path == "0" and paths["2"].alternative_path == "0"

    assert len(p.body_groups) == 1
    group = p.body_groups[0]
    assert group.alternative_path == "0"
    assert group.compared is True
    assert {(v.mime_path, v.text_source) for v in group.versions} == {
        ("1", TextSource.PLAIN_PART),
        ("2", TextSource.HTML_DERIVED),
    }


def test_only_parts_under_same_alternative_are_grouped():
    # multibyte sample: alternative with two plain + nested related html;
    # plus a standalone embedded message/rfc822 text body.
    p = parse_eml((SAMPLES / "01_multibyte.eml").read_bytes())
    alt_groups = [g for g in p.body_groups if g.alternative_path is not None]
    standalone = [g for g in p.body_groups if g.alternative_path is None]
    assert len(alt_groups) == 1
    assert {v.mime_path for v in alt_groups[0].versions} == {"1.1", "1.2", "1.3.1"}
    assert {v.mime_path for g in standalone for v in g.versions} == {"2.1"}
    # The embedded text body must never be paired with the outer alternative.
    assert all(g.compared is False for g in standalone)


def test_paragraph_diff_is_explainable_and_locatable():
    p = parse_eml((SAMPLES / "10_alt_divergent.eml").read_bytes())
    rows = p.body_groups[0].paragraph_diffs
    kinds = {r.kind for r in rows}
    assert {"equal", "replaced", "plain_only", "html_only"} <= kinds
    equal = next(r for r in rows if r.kind == "equal")
    assert equal.left_path == "1" and equal.right_path == "2"
    assert equal.left_text == equal.right_text == "Quarterly meeting notes."
    # Reworded-but-related paragraph is a replacement on both sides.
    replaced = next(r for r in rows if r.kind == "replaced")
    assert "north hall" in replaced.left_text and "south hall" in replaced.right_text
    # Unrelated unique lines stay one-sided — no fabricated correspondence.
    assert any(r.kind == "plain_only" and "print this email" in (r.left_text or "") for r in rows)
    html_only = next(r for r in rows if r.kind == "html_only")
    assert "ZEPPELINUNIQUE" in (html_only.right_text or "")
    # Nothing in the diff rows carries raw HTML tags.
    for r in rows:
        for side in (r.left_text, r.right_text):
            assert "<" not in (side or "")


def test_unrelated_paragraphs_are_not_called_replacements():
    p = parse_eml((SAMPLES / "10_alt_divergent.eml").read_bytes())
    plain, html = p.bodies
    rows = compare_versions(plain, html)
    # the print instruction vs the html-only line must be one-sided rows
    assert not any(
        r.kind == "replaced"
        and "print this email" in (r.left_text or "")
        and "ZEPPELINUNIQUE" in (r.right_text or "")
        for r in rows
    )


def test_split_paragraphs_normalizes_crlf_and_blocks():
    assert split_paragraphs("a\r\n\r\nb\r\nc") == ["a", "b", "c"]
    assert split_paragraphs("<p>x</p>\n<p>y</p>") == ["<p>x</p>", "<p>y</p>"]


# ---------- search provenance / safe snippets ------------------------------


def test_html_only_word_locates_html_derived_version():
    p = parse_eml((SAMPLES / "10_alt_divergent.eml").read_bytes())
    hits = body_matches(_body_rows(p), "ZEPPELINUNIQUE")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["mime_path"] == "2"
    assert hit["content_type"] == "text/html"
    assert hit["text_source"] == TextSource.HTML_DERIVED.value
    assert "ZEPPELINUNIQUE" in hit["snippet"]
    assert "<" not in hit["snippet"]  # plain text snippet, no markup at all


def test_shared_word_returns_both_sources():
    p = parse_eml((SAMPLES / "10_alt_divergent.eml").read_bytes())
    hits = body_matches(_body_rows(p), "quarterly")
    assert len(hits) == 2
    assert {h["mime_path"] for h in hits} == {"1", "2"}
    assert {h["text_source"] for h in hits} == {"plain_part", "html_derived"}


def test_script_content_is_not_searchable_and_snippets_are_escaped():
    p = parse_eml((SAMPLES / "09_html_xss.eml").read_bytes())
    rows = _body_rows(p)
    # Payload words inside <script> never appear in derived text → no hits.
    for payload in ("document.cookie", "fetch(", "alert(1)"):
        assert body_matches(rows, payload) == []
    # Visible text is still searchable.
    hits = body_matches(rows, "hello")
    assert len(hits) == 1 and hits[0]["text_source"] == "html_derived"
    # Snippets escape anything that looks like markup.
    snip = make_snippet("<script>x</script> b <b>bold</b>", "bold")
    assert "<script" not in snip and "<b>" not in snip
    assert "&lt;b&gt;" in snip


# ---------- API level dedup / detail / safety ------------------------------


def _post(client, name, data):
    c, _ = client
    return c.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
    )


def test_search_api_one_message_per_mail_with_two_body_sources(client):
    c, _ = client
    data = (SAMPLES / "10_alt_divergent.eml").read_bytes()
    r = _post(client, "10_alt_divergent.eml", data)
    assert r.status_code == 201
    pk = r.json()["message_pk"]

    shared = c.get("/search", params={"q": "quarterly"}).json()
    assert shared["count"] == 1
    assert shared["results"][0]["id"] == pk
    sources = {(h["mime_path"], h["text_source"]) for h in shared["results"][0]["body_matches"]}
    assert sources == {("1", "plain_part"), ("2", "html_derived")}

    only = c.get("/search", params={"q": "ZEPPELINUNIQUE"}).json()
    assert only["count"] == 1
    hit = only["results"][0]["body_matches"][0]
    assert hit["mime_path"] == "2" and hit["text_source"] == "html_derived"
    assert "ZEPPELINUNIQUE" in hit["snippet"]
    assert "<script" not in hit["snippet"]


def test_message_detail_shows_versions_and_diffs(client):
    c, _ = client
    _post(client, "d.eml", (SAMPLES / "10_alt_divergent.eml").read_bytes())
    pk = c.get("/search", params={"q": "ZEPPELINUNIQUE"}).json()["results"][0]["id"]
    detail = c.get(f"/messages/{pk}").json()

    assert detail["body_groups"], "detail must expose reconciliation groups"
    group = detail["body_groups"][0]
    assert group["status"] == "divergent"
    assert group["provenance"] == "stored"
    assert {v["mime_path"] for v in group["versions"]} == {"1", "2"}
    kinds = {d["kind"] for d in group["paragraph_diffs"]}
    assert {"equal", "replaced", "plain_only", "html_only"} <= kinds
    for body in detail["bodies"]:
        assert body["text_source"] in ("plain_part", "html_derived")
        if body["content_type"] == "text/html":
            assert "<script" not in (body["safe_html"] or "")
            assert "&lt;script&gt;" in body["escaped_html"]


def test_xss_html_never_executes_in_snippet_or_detail(client):
    c, _ = client
    _post(client, "x.eml", (SAMPLES / "09_html_xss.eml").read_bytes())
    for payload in ("document.cookie", "fetch(", "alert(1)"):
        assert c.get("/search", params={"q": payload}).json()["count"] == 0
    hits = c.get("/search", params={"q": "hello"}).json()["results"]
    assert len(hits) == 1
    snippet = hits[0]["body_matches"][0]["snippet"]
    assert "<script" not in snippet and "onerror" not in snippet
    assert "fetch" not in snippet

    pk = hits[0]["id"]
    detail = c.get(f"/messages/{pk}").json()
    html_body = next(b for b in detail["bodies"] if b["content_type"] == "text/html")
    assert "<script" not in html_body["safe_html"]
    assert "onerror" not in html_body["safe_html"] and "onclick" not in html_body["safe_html"]
    assert "javascript:" not in html_body["safe_html"]
    assert "document.cookie" not in (html_body["plain_text"] or "")
    # the fully-escaped rendering keeps the raw characters inert
    assert "&lt;script&gt;" in html_body["escaped_html"]


def test_legacy_message_without_original_parts_marked_source_unknown(client):
    c, arch = client
    _post(client, "d.eml", (SAMPLES / "10_alt_divergent.eml").read_bytes())
    pk = c.get("/search", params={"q": "quarterly"}).json()["results"][0]["id"]

    # Simulate a pre-feature record: no stored reconciliation, no provenance
    # columns, and the MIME tree gone — original parts cannot be recovered.
    repo = arch.repo
    repo.messages[pk]["body_groups"] = None
    repo.messages[pk]["tree_json"] = None
    for row in repo.bodies:
        if row["message_pk"] == pk:
            row.pop("text_source", None)
            row.pop("alt_relation", None)
            row.pop("alternative_path", None)

    detail = c.get(f"/messages/{pk}").json()
    assert detail["body_groups"]
    for group in detail["body_groups"]:
        assert group["provenance"] == "unknown"
        assert group["status"] == "source_unknown"
        assert group["compared"] is False
        assert group["paragraph_diffs"] == []
        for version in group["versions"]:
            assert version["text_source"] == "unknown"
            assert version["relation"] == "unknown"


def test_legacy_message_reconstructs_when_original_part_facts_survive(client):
    c, arch = client
    _post(client, "d.eml", (SAMPLES / "10_alt_divergent.eml").read_bytes())
    pk = c.get("/search", params={"q": "quarterly"}).json()["results"][0]["id"]

    repo = arch.repo
    # Drop the stored reconciliation/provenance but keep mime paths, content
    # types and the MIME tree — these are original-part facts.
    repo.messages[pk]["body_groups"] = None
    for row in repo.bodies:
        if row["message_pk"] == pk:
            row["text_source"] = "unknown"
            row["alt_relation"] = "unknown"
            row["alternative_path"] = None

    detail = c.get(f"/messages/{pk}").json()
    group = detail["body_groups"][0]
    assert group["provenance"] == "reconstructed"
    assert group["compared"] is True
    assert {v["text_source"] for v in group["versions"]} == {"plain_part", "html_derived"}
    assert {d["kind"] for d in group["paragraph_diffs"]} >= {
        "equal", "replaced", "plain_only", "html_only"
    }
