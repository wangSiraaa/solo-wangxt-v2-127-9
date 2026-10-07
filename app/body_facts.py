"""Readable-body provenance, group views and safe search snippets.

This module is the single place that turns parser body facts
(:class:`~app.parser.models.BodyPart` / :class:`~app.parser.models.BodyGroup`)
into JSON for persistence/API responses, and that *reconstructs* them from
stored rows when reading older records.

Hard rules:

* snippets and diffs are plain text only — HTML-derived versions contribute
  the sanitizer's extracted text, raw HTML is never emitted into a snippet;
* every snippet is fully HTML-escaped, so it cannot carry executable markup
  even if a client renders it carelessly;
* provenance that cannot be established from original parts is reported as
  ``unknown`` ("来源未知") — versions are never paired or diffed on a guess.
"""
from __future__ import annotations

from typing import Any, Iterable

from .body_compare import build_body_groups, diff_summary
from .parser.html_sanitizer import escape_html
from .parser.models import (
    AltRelation,
    BodyGroup,
    BodyPart,
    ContentDisposition,
    TextSource,
)

SNIPPET_RADIUS = 80

SOURCE_UNKNOWN = TextSource.UNKNOWN.value
RELATION_UNKNOWN = AltRelation.UNKNOWN.value

# Body types whose text we ever index, diff or show in snippets.
_READABLE_TYPES = ("text/plain", "text/html")


# -- write side: parser objects -> JSON --------------------------------------


def body_row_extras(body: BodyPart) -> dict[str, Any]:
    """Provenance columns stored alongside every body part row."""
    return {
        "text_source": body.text_source.value,
        "alt_relation": body.alt_relation.value,
        "alternative_path": body.alternative_path,
    }


def _version_json(version: Any) -> dict[str, Any]:
    return {
        "mime_path": version.mime_path,
        "content_type": version.content_type,
        "relation": version.relation.value,
        "text_source": version.text_source.value,
    }


def _diff_json(row: Any) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "left_path": row.left_path,
        "right_path": row.right_path,
        "left_text": row.left_text,
        "right_text": row.right_text,
    }


def groups_to_json(groups: Iterable[BodyGroup]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for group in groups:
        out.append(
            {
                "group_key": group.group_key,
                "alternative_path": group.alternative_path,
                "provenance": "stored",
                "versions": [_version_json(v) for v in group.versions],
                "compared": group.compared,
                "paragraph_diffs": [_diff_json(d) for d in group.paragraph_diffs],
            }
        )
    return out


# -- read side: normalize stored rows ----------------------------------------


def normalize_body_rows(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Guarantee provenance keys exist; pre-feature rows default to unknown."""
    for body in bodies:
        body.setdefault("text_source", SOURCE_UNKNOWN)
        body.setdefault("alt_relation", RELATION_UNKNOWN)
        body.setdefault("alternative_path", None)
    return bodies


def body_searchable_text(body_row: dict[str, Any]) -> str:
    """Plain text ever searched/snippet-shown for a stored body row.

    Prefers the extracted ``plain_text`` (script-free for HTML); falls back to
    the plain-part ``text``. Raw HTML is never returned here.
    """
    if body_row.get("plain_text"):
        return body_row["plain_text"] or ""
    if body_row.get("content_type") == "text/plain":
        return body_row.get("text") or ""
    return ""


def _rows_to_body_parts(bodies: list[dict[str, Any]]) -> list[BodyPart]:
    """Reconstruct lightweight BodyPart objects from surviving original facts.

    Content type and MIME path are original-part facts, so the relation and
    text source inferred from them are trustworthy; the body rows' own
    (possibly default-unknown) provenance columns are left untouched.
    """
    parts: list[BodyPart] = []
    for row in bodies:
        ctype = row.get("content_type") or ""
        if ctype not in _READABLE_TYPES:
            continue
        plain = body_searchable_text(row)
        relation = AltRelation.PLAIN if ctype == "text/plain" else AltRelation.HTML
        source = TextSource.PLAIN_PART if ctype == "text/plain" else TextSource.HTML_DERIVED
        parts.append(
            BodyPart(
                mime_path=row["mime_path"],
                content_type=ctype,
                charset=row.get("charset"),
                declared_charset=row.get("declared_charset"),
                disposition=ContentDisposition.UNKNOWN,
                content_id=row.get("content_id"),
                content_location=row.get("content_location"),
                byte_size=row.get("byte_size") or len(plain.encode("utf-8")),
                # For text/plain the stored text is the part text; for
                # text/html only the extracted text survived, and raw HTML is
                # intentionally left unavailable.
                text=plain,
                safe_html=row.get("safe_html"),
                plain_text=plain,
                text_source=source,
                alt_relation=relation,
            )
        )
    return parts


def _group_from_stored(raw: dict[str, Any]) -> dict[str, Any]:
    versions = []
    for version in raw.get("versions") or []:
        versions.append(
            {
                "mime_path": version.get("mime_path"),
                "content_type": version.get("content_type"),
                "relation": version.get("relation") or RELATION_UNKNOWN,
                "text_source": version.get("text_source") or SOURCE_UNKNOWN,
            }
        )
    diffs = [
        {
            "kind": d.get("kind"),
            "left_path": d.get("left_path"),
            "right_path": d.get("right_path"),
            "left_text": d.get("left_text"),
            "right_text": d.get("right_text"),
        }
        for d in (raw.get("paragraph_diffs") or [])
    ]
    return {
        "group_key": raw.get("group_key"),
        "alternative_path": raw.get("alternative_path"),
        "provenance": "stored",
        "versions": versions,
        "compared": bool(raw.get("compared")) and bool(diffs),
        "paragraph_diffs": diffs,
    }


def _group_view(group: BodyGroup, provenance: str) -> dict[str, Any]:
    return {
        "group_key": group.group_key,
        "alternative_path": group.alternative_path,
        "provenance": provenance,
        "versions": [_version_json(v) for v in group.versions],
        "compared": group.compared,
        "paragraph_diffs": [_diff_json(d) for d in group.paragraph_diffs],
    }


def _unknown_groups(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback for records whose original parts cannot be obtained.

    Each readable row is reported on its own with unknown provenance; no
    pairing and no diff is produced.
    """
    groups: list[dict[str, Any]] = []
    for row in bodies:
        if (row.get("content_type") or "") not in _READABLE_TYPES:
            continue
        groups.append(
            {
                "group_key": f"unknown:{row.get('mime_path') or id(row)}",
                "alternative_path": None,
                "provenance": "unknown",
                "versions": [
                    {
                        "mime_path": row.get("mime_path"),
                        "content_type": row.get("content_type"),
                        "relation": RELATION_UNKNOWN,
                        "text_source": SOURCE_UNKNOWN,
                    }
                ],
                "compared": False,
                "paragraph_diffs": [],
            }
        )
    return groups


def assemble_body_groups(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach reconciled body-version groups to a stored message dict.

    Order of trust: stored reconciliation (new records) → honest
    reconstruction from original part facts (mime paths, content types and the
    MIME tree) → explicit ``unknown`` provenance when those facts are missing.
    """
    bodies = normalize_body_rows(list(message.get("bodies") or []))
    stored = message.get("body_groups")
    if stored:
        groups = [_group_from_stored(g) for g in stored]
    else:
        tree = message.get("tree_json")
        can_reconstruct = bool(tree) and all(
            row.get("mime_path") and (row.get("content_type") or "") in _READABLE_TYPES
            for row in bodies
            if (row.get("content_type") or "") in _READABLE_TYPES
        ) and any((row.get("content_type") or "") in _READABLE_TYPES for row in bodies)
        if can_reconstruct:
            from app.parser.models import ContentDisposition, PartNode

            def _node(raw: dict[str, Any]) -> Any:
                disp = raw.get("disposition")
                try:
                    disposition = ContentDisposition(disp) if disp else ContentDisposition.UNKNOWN
                except ValueError:
                    disposition = ContentDisposition.UNKNOWN
                return PartNode(
                    mime_path=raw["mime_path"],
                    content_type=raw["content_type"],
                    disposition=disposition,
                    filename=raw.get("filename"),
                    is_multipart=raw.get("is_multipart", False),
                    is_embedded_message=raw.get("is_embedded_message", False),
                    byte_size=raw.get("byte_size", 0),
                    children=[_node(c) for c in raw.get("children") or []],
                )

            part_tree = _node(tree)
            rebuilt_parts = _rows_to_body_parts(bodies)
            rebuilt = build_body_groups(part_tree, rebuilt_parts)
            groups = [_group_view(g, "reconstructed") for g in rebuilt]
            # Surface the inference on the per-part view as well. The stored
            # columns themselves are not rewritten (they were never recorded).
            inferred = {p.mime_path: p for p in rebuilt_parts}
            for row in bodies:
                part = inferred.get(row.get("mime_path"))
                if part is not None:
                    row["alt_relation"] = part.alt_relation.value
                    row["text_source"] = part.text_source.value
                    row["alternative_path"] = part.alternative_path
        else:
            groups = _unknown_groups(bodies)

    for group in groups:
        group["summary"] = diff_summary(group["paragraph_diffs"])
        if not group["compared"]:
            group["status"] = "unpaired" if group["provenance"] != "unknown" else "source_unknown"
        elif group["summary"]["equal"] == sum(group["summary"].values()):
            group["status"] = "identical"
        else:
            group["status"] = "divergent"
    message["body_groups"] = groups
    return groups


# -- search side -------------------------------------------------------------


def make_snippet(text: str, query_lower: str, radius: int = SNIPPET_RADIUS) -> str:
    """Return an escaped, single-line, truncated window around the first hit.

    Works on plain text only; newlines collapse to spaces and the whole window
    is entity-escaped, so no markup (script tag or otherwise) can survive.
    """
    lowered = text.lower()
    idx = lowered.find(query_lower)
    if idx < 0:
        idx = 0
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query_lower) + radius)
    window = text[start:end]
    window = " ".join(window.split())  # collapse all whitespace incl. newlines
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + escape_html(window) + suffix


def _provenance(row: dict[str, Any], field: str, inferred: dict[str, str]) -> str:
    """Prefer a recorded provenance value; fall back to inference/unknown."""
    recorded = row.get(field)
    if recorded and recorded not in (SOURCE_UNKNOWN, RELATION_UNKNOWN):
        return recorded
    return inferred[field]


def body_matches(bodies: Iterable[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """List every readable version containing ``query`` (one entry per part).

    Duplicate words across plain and HTML therefore surface as two sources on
    the same message; the message itself stays a single search result.

    Provenance uses the recorded value when present. For legacy rows it is
    inferred from the surviving original-part fact (content type) when that is
    trustworthy, and reported ``unknown`` otherwise — never fabricated.
    """
    q = query.lower()
    matches: list[dict[str, Any]] = []
    for row in bodies:
        ctype = row.get("content_type") or ""
        if ctype not in _READABLE_TYPES:
            continue
        searchable = body_searchable_text(row)
        if not searchable or q not in searchable.lower():
            continue
        if ctype == "text/plain":
            inferred = {"text_source": TextSource.PLAIN_PART.value,
                        "alt_relation": AltRelation.PLAIN.value}
        elif ctype == "text/html":
            inferred = {"text_source": TextSource.HTML_DERIVED.value,
                        "alt_relation": AltRelation.HTML.value}
        else:
            inferred = {"text_source": SOURCE_UNKNOWN, "alt_relation": RELATION_UNKNOWN}
        matches.append(
            {
                "mime_path": row.get("mime_path"),
                "content_type": ctype,
                "relation": _provenance(row, "alt_relation", inferred),
                "text_source": _provenance(row, "text_source", inferred),
                "match_count": searchable.lower().count(q),
                "snippet": make_snippet(searchable, q),
            }
        )
    matches.sort(key=lambda m: (m["mime_path"] or ""))
    return matches
