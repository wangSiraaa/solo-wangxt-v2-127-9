"""Reconcile the readable body versions of a parsed message.

The same message can carry a ``text/plain`` and a ``text/html`` alternative
whose contents disagree. This module:

* keeps, for every readable body part, its MIME path and the *text source*
  (the plain part body vs. text derived from the HTML part — never raw HTML);
* groups versions by the only relationship the MIME structure asserts — a
  common nearest ``multipart/alternative`` ancestor — and produces an
  explainable paragraph-level diff;
* builds safe search snippets from readable plain text only.

Legacy body rows for which the originating part is unavailable are reported as
``来源未知`` rather than guessed: no correspondence is fabricated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .models import (
    BodyDiff,
    BodyPart,
    BodyVersion,
    ContentDisposition,
    ParagraphNote,
    PartNode,
    TextSource,
)

# Provenance labels (Chinese, surfaced verbatim to API clients).
LABEL_PLAIN = "text/plain 原始正文"
LABEL_HTML_DERIVED = "text/html 派生文本"
LABEL_UNKNOWN = "来源未知"

# Bounds keep explainability bounded for pathological bodies.
MAX_NOTES = 50
MAX_PARAGRAPH_CHARS = 500
SNIPPET_RADIUS = 80
SNIPPET_MAX = 200

_WS_RE = re.compile(r"\s+")


def source_label(source: TextSource, mime_path: str | None) -> str:
    """Human-readable provenance label; legacy/unknown parts stay explicit."""
    if source is TextSource.UNKNOWN or not mime_path:
        return LABEL_UNKNOWN
    base = LABEL_PLAIN if source is TextSource.PLAIN else LABEL_HTML_DERIVED
    return f"{base} (MIME {mime_path})"


def split_paragraphs(text: str | None) -> list[str]:
    """Split readable text into normalized, deduplicated paragraphs.

    Normalization (whitespace collapse) lets a plain part and an HTML-derived
    rendition compare equal for content even when the markup introduced line
    breaks. Blank lines and repeats are dropped; order is first-seen.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        line = _WS_RE.sub(" ", raw).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line[:MAX_PARAGRAPH_CHARS])
    return out


def version_from_part(part: BodyPart) -> BodyVersion:
    """Build a provenance-tracked version from a parsed body part."""
    paras = split_paragraphs(part.plain_text if part.text_source is TextSource.HTML_DERIVED else part.text)
    return BodyVersion(
        mime_path=part.mime_path or None,
        content_type=part.content_type,
        text_source=part.text_source,
        source_label=source_label(part.text_source, part.mime_path or None),
        charset=part.charset,
        paragraphs=len(paras),
        chars=sum(len(p) for p in paras),
        known=True,
    )


def version_from_row(row: dict[str, Any]) -> BodyVersion:
    """Build a version from a stored body row.

    Rows without a usable content type/MIME path are legacy records: the
    readable text (if any) is surfaced as ``来源未知`` rather than attributed
    to a plain or HTML part that no longer exists.
    """
    mime_path = row.get("mime_path") or None
    ctype = (row.get("content_type") or "").lower() or None
    if ctype == "text/html":
        source = TextSource.HTML_DERIVED
        text = row.get("plain_text")
    elif ctype == "text/plain":
        source = TextSource.PLAIN
        text = row.get("plain_text") if row.get("plain_text") is not None else row.get("text")
    else:
        source = TextSource.UNKNOWN
        text = row.get("plain_text")
    paras = split_paragraphs(text)
    return BodyVersion(
        mime_path=mime_path,
        content_type=ctype,
        text_source=source,
        source_label=source_label(source, mime_path),
        charset=row.get("charset"),
        paragraphs=len(paras),
        chars=sum(len(p) for p in paras),
        known=source is not TextSource.UNKNOWN,
    )


# --------------------------------------------------------------------------
# MIME-tree grouping: only a shared multipart/alternative ancestor relates
# body versions to each other.
# --------------------------------------------------------------------------


def _index_tree(tree: PartNode | None) -> tuple[dict[str, PartNode], dict[str, str | None]]:
    """Return (nodes by path, nearest multipart/alternative ancestor by path).

    Crossing an embedded message boundary resets ancestry: an alternative
    inside a forwarded message/rfc822 must not group with the outer message.
    """
    nodes: dict[str, PartNode] = {}
    alt_of: dict[str, str | None] = {}
    if tree is None:
        return nodes, alt_of

    def recurse(node: PartNode, alt_ancestor: str | None) -> None:
        nodes[node.mime_path] = node
        alt_of[node.mime_path] = alt_ancestor
        current_alt = (
            node.mime_path
            if node.content_type.lower() == "multipart/alternative"
            else alt_ancestor
        )
        for child in node.children:
            # Embedded messages own their structure: start a fresh lineage.
            child_alt = current_alt if not child.is_embedded_message else None
            recurse(child, child_alt)

    recurse(tree, None)
    return nodes, alt_of


def _group_keys(tree: PartNode | None, paths: Sequence[str]) -> dict[str, str]:
    _, alt_of = _index_tree(tree)
    groups: dict[str, str] = {}
    for path in paths:
        alt = alt_of.get(path)
        groups[path] = alt or path or "unknown"
    return groups


def _make_diff(group_key: str, alt_path: str | None, versions: list[BodyVersion], paras: dict[str, list[str]]) -> BodyDiff:
    keys = [v.mime_path or "unknown" for v in versions]
    presence: dict[str, set[str]] = {}
    order: list[str] = []
    for key in keys:
        for para in paras.get(key, []):
            if para not in presence:
                presence[para] = set()
                order.append(para)
            presence[para].add(key)

    shared: list[str] = []
    unique: list[ParagraphNote] = []
    partial: list[ParagraphNote] = []
    n = len(keys)
    for para in order:
        where = presence[para]
        if len(where) == n and n > 1:
            shared.append(para)
        elif len(where) == 1:
            unique.append(ParagraphNote(paragraph=para, present_in=[next(iter(where))]))
        else:
            partial.append(ParagraphNote(paragraph=para, present_in=sorted(where)))

    return BodyDiff(
        group_key=group_key,
        alternative_path=alt_path,
        relation="alternative" if alt_path else "standalone",
        versions=versions,
        # Standalone groups have nothing to agree or disagree with; agreement is
        # only meaningful across alternative renditions.
        agreed=(not unique and not partial) if alt_path else True,
        shared=shared[:MAX_NOTES],
        unique=unique[:MAX_NOTES],
        partial=partial[:MAX_NOTES],
    )


def reconcile_parts(parts: Sequence[BodyPart], tree: PartNode | None) -> tuple[list[BodyVersion], list[BodyDiff]]:
    """Reconcile parsed body parts using the structural MIME tree."""
    versions = [version_from_part(p) for p in parts]
    paths = [p.mime_path or "" for p in parts]
    groups = _group_keys(tree, paths)

    grouped: dict[str, list[tuple[BodyPart, BodyVersion]]] = {}
    order: list[str] = []
    for part, ver in zip(parts, versions):
        key = groups.get(part.mime_path or "", part.mime_path or "unknown")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append((part, ver))

    diffs: list[BodyDiff] = []
    for key in order:
        members = grouped[key]
        # The group key is the alternative container path exactly when members
        # report that path as their ancestor (their own path differs).
        is_alt = any((p.mime_path or "") != key for p, _ in members)
        alt_path = key if is_alt else None
        members.sort(key=lambda pv: (pv[0].mime_path or "", pv[0].content_type))
        vers = [v for _, v in members]
        paras = {
            (v.mime_path or "unknown"): split_paragraphs(
                p.plain_text if v.text_source is TextSource.HTML_DERIVED else p.text
            )
            for p, v in members
        }
        diffs.append(_make_diff(key, alt_path, vers, paras))
    return versions, diffs


def reconcile_rows(rows: Sequence[dict[str, Any]], tree: PartNode | None) -> tuple[list[BodyVersion], list[BodyDiff]]:
    """Reconcile stored body rows (read path; handles legacy unknown rows)."""
    versions = [version_from_row(r) for r in rows]
    paths = [(r.get("mime_path") or "") for r in rows]
    groups = _group_keys(tree, paths)

    grouped: dict[str, list[tuple[dict[str, Any], BodyVersion]]] = {}
    order: list[str] = []
    unknown_seq = 0
    for row, ver in zip(rows, versions):
        path = row.get("mime_path") or ""
        if not ver.known:
            # Unknown rows never share a group with anyone — not even each
            # other: without originating parts a relationship cannot be known.
            unknown_seq += 1
            key = f"unknown:{path or '?'}#{unknown_seq}"
        else:
            key = groups.get(path, path)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append((row, ver))

    diffs: list[BodyDiff] = []
    for key in order:
        members = grouped[key]
        # Same rule as reconcile_parts: an alternative container group has a
        # key different from every member's own path.
        is_alt = any(v.known and (r.get("mime_path") or "") != key for r, v in members)
        alt_path = key if is_alt else None
        members.sort(key=lambda rv: (rv[0].get("mime_path") or "", rv[0].get("content_type") or ""))
        vers = [v for _, v in members]
        paras = {
            (v.mime_path or "unknown"): split_paragraphs(_row_readable_text(r))
            for r, v in members
        }
        diffs.append(_make_diff(key, alt_path, vers, paras))
    return versions, diffs


def _row_readable_text(row: dict[str, Any]) -> str | None:
    """Readable text for a stored row. HTML rows expose derived text only."""
    ctype = (row.get("content_type") or "").lower()
    if ctype == "text/html":
        return row.get("plain_text")
    if ctype == "text/plain":
        return row.get("plain_text") if row.get("plain_text") is not None else row.get("text")
    return row.get("plain_text")


# --------------------------------------------------------------------------
# Search snippets — readable plain text, never raw/sanitized HTML.
# --------------------------------------------------------------------------


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def safe_snippet(text: str | None, query: str) -> str | None:
    """Return a bounded, whitespace-collapsed snippet around a query hit.

    The input must already be readable plain text (``plain_text``). No HTML is
    ever passed in here, so the result cannot execute or carry markup; it is
    returned as plain JSON text.
    """
    if not text or not query:
        return None
    haystack = normalize_ws(text)
    needle = normalize_ws(query)
    if not needle:
        return None
    idx = haystack.lower().find(needle.lower())
    if idx < 0:
        return None
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(haystack), idx + len(needle) + SNIPPET_RADIUS)
    snippet = haystack[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(haystack):
        snippet = snippet + "…"
    if len(snippet) > SNIPPET_MAX:
        snippet = snippet[:SNIPPET_MAX].rstrip() + "…"
    return snippet


@dataclass(frozen=True)
class BodyMatch:
    """One body-version search hit: which version and a safe snippet."""

    mime_path: str | None
    content_type: str | None
    text_source: str
    source_label: str
    known: bool
    snippet: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mime_path": self.mime_path,
            "content_type": self.content_type,
            "text_source": self.text_source,
            "source_label": self.source_label,
            "known": self.known,
            "snippet": self.snippet,
        }


def match_body_rows(rows: Iterable[dict[str, Any]], query: str) -> list[BodyMatch]:
    """Find which readable body versions contain ``query`` (case-insensitive).

    Each version contributes at most one match even if the term repeats, so a
    message with the word in both plain and HTML-derived text lists two
    sources while remaining a single search result.
    """
    needle = normalize_ws(query).lower()
    if not needle:
        return []
    hits: list[BodyMatch] = []
    for row in rows:
        text = _row_readable_text(row)
        if not text or needle not in normalize_ws(text).lower():
            continue
        ver = version_from_row(row)
        hits.append(
            BodyMatch(
                mime_path=ver.mime_path,
                content_type=ver.content_type,
                text_source=ver.text_source.value,
                source_label=ver.source_label,
                known=ver.known,
                snippet=safe_snippet(text, query),
            )
        )
    return hits


def diff_to_json(diff: BodyDiff) -> dict[str, Any]:
    """JSON-friendly rendering for the detail API (plain text only)."""
    return {
        "group_key": diff.group_key,
        "alternative_path": diff.alternative_path,
        "relation": diff.relation,
        "agreed": diff.agreed,
        "versions": [version_to_json(v) for v in diff.versions],
        "paragraph_summary": {
            "shared": diff.shared,
            "only_in": [note.__dict__ for note in diff.unique],
            "partial": [note.__dict__ for note in diff.partial],
        },
    }


def version_to_json(ver: BodyVersion) -> dict[str, Any]:
    return {
        "mime_path": ver.mime_path,
        "content_type": ver.content_type,
        "text_source": ver.text_source.value,
        "source_label": ver.source_label,
        "charset": ver.charset,
        "paragraphs": ver.paragraphs,
        "chars": ver.chars,
        "known": ver.known,
    }


def reconcile_for_detail(rows: Sequence[dict[str, Any]], tree: PartNode | None) -> dict[str, Any]:
    """Compute the ``body_versions`` / ``body_diffs`` payload for a detail row."""
    versions, diffs = reconcile_rows(rows, tree)
    return {
        "body_versions": [version_to_json(v) for v in versions],
        "body_diffs": [diff_to_json(d) for d in diffs],
    }


def tree_from_json(data: dict[str, Any] | None) -> PartNode | None:
    """Rebuild a structural tree from the stored ``tree_json`` (read path)."""
    if not data:
        return None

    def build(node: dict[str, Any]) -> PartNode:
        return PartNode(
            mime_path=node["mime_path"],
            content_type=node.get("content_type") or "application/octet-stream",
            disposition=ContentDisposition(node.get("disposition") or "unknown"),
            filename=node.get("filename"),
            is_multipart=bool(node.get("is_multipart")),
            is_embedded_message=bool(node.get("is_embedded_message")),
            byte_size=int(node.get("byte_size") or 0),
            children=[build(c) for c in node.get("children") or []],
        )

    return build(data)
