"""Cross-version body reconciliation.

The ``text/plain`` and ``text/html`` alternatives of one multipart message are
supposed to carry the same content; in practice they routinely diverge. This
module:

* groups the readable body parts by their *real* MIME nesting — only parts
  inside the same ``multipart/alternative`` container are treated as versions
  of the same logical body (we never invent a correspondence between unrelated
  parts);
* extracts the searchable text of each version with its provenance (the raw
  text of a text/plain part vs. text *derived* from a text/html part);
* aligns the versions paragraph by paragraph (``difflib``) and emits
  human-readable, locatable diff rows.

Nothing here ever touches raw HTML: HTML versions contribute only the
script-free text produced by :func:`app.parser.html_sanitizer.strip_to_text`.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

from .parser.models import (
    AltRelation,
    BodyGroup,
    BodyPart,
    BodyVersionRef,
    ParagraphDiff,
    PartNode,
    TextSource,
)

_PARA_SPLIT_RE = re.compile(r"\n+")
_WS_RE = re.compile(r"[^\S\n]+")  # spaces/tabs/NBSP, but not newlines


def readable_text(body: BodyPart) -> str:
    """Searchable/diffable text of one body part.

    text/plain contributes its decoded text; text/html contributes only the
    extracted, script-free plain text.
    """
    if body.content_type == "text/html":
        return body.plain_text or ""
    return body.text or ""


def relation_of(body: BodyPart) -> AltRelation:
    if body.content_type == "text/plain":
        return AltRelation.PLAIN
    if body.content_type == "text/html":
        return AltRelation.HTML
    return AltRelation.OTHER


def source_of(body: BodyPart) -> TextSource:
    if body.content_type == "text/html":
        return TextSource.HTML_DERIVED
    if body.content_type == "text/plain":
        return TextSource.PLAIN_PART
    return TextSource.UNKNOWN


def split_paragraphs(text: str) -> list[str]:
    """Split readable text into normalized paragraphs for comparison.

    Blank lines and single newlines both separate paragraphs (HTML extraction
    emits one newline per block tag; plain parts use CRLF runs). Internal
    whitespace runs (incl. NBSP) collapse to a single space; empties dropped.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    for block in _PARA_SPLIT_RE.split(text):
        joined = _WS_RE.sub(" ", block).strip()
        if joined:
            paragraphs.append(joined)
    return paragraphs


# -- MIME grouping ----------------------------------------------------------


def _alt_member_paths(tree: PartNode) -> dict[str, str]:
    """Map each body-part mime path to its nearest enclosing alternative.

    A nested ``multipart/alternative`` starts its own group, so parts under it
    are never attributed to the outer container.
    """
    member_of: dict[str, str] = {}

    def walk(node: PartNode, enclosing: str | None) -> None:
        if node.content_type == "multipart/alternative":
            enclosing = node.mime_path
        if enclosing is not None:
            member_of[node.mime_path] = enclosing
        for child in node.children:
            walk(child, enclosing)

    walk(tree, None)
    return member_of


def build_body_groups(tree: PartNode | None, bodies: list[BodyPart]) -> list[BodyGroup]:
    """Group readable parts into alternative sets and compute paragraph diffs.

    Also annotates each :class:`BodyPart` with its provenance/relation fields.
    Parts outside any alternative container become explicit singleton groups;
    no part is silently dropped and no pair is ever fabricated.
    """
    for body in bodies:
        body.text_source = source_of(body)
        body.alt_relation = relation_of(body)

    if tree is None:
        groups = [
            _singleton(body)
            for body in bodies
            if body.alt_relation is not AltRelation.OTHER
        ]
        return _ordered(bodies, groups)

    member_of = _alt_member_paths(tree)
    buckets: dict[str, list[BodyPart]] = {}
    for body in bodies:
        body.alternative_path = member_of.get(body.mime_path)
        if body.alt_relation is AltRelation.OTHER:
            continue
        alt_path = member_of.get(body.mime_path)
        if alt_path is None:
            continue
        buckets.setdefault(alt_path, []).append(body)

    groups: list[BodyGroup] = []
    # MIME order of the alternatives themselves (shallowest path first).
    for alt_path in sorted(buckets, key=_path_sort_key):
        members = sorted(buckets[alt_path], key=lambda b: _path_sort_key(b.mime_path))
        group = BodyGroup(
            group_key=f"alternative:{alt_path}",
            alternative_path=alt_path,
            versions=[_version_ref(b) for b in members],
        )
        left, right = _comparison_pair(members)
        if left is not None and right is not None:
            group.paragraph_diffs = compare_versions(left, right)
            group.compared = True
        groups.append(group)

    # Readable parts with no alternative ancestor remain standalone facts.
    grouped_paths = {b.mime_path for members in buckets.values() for b in members}
    for body in bodies:
        if body.alt_relation is not AltRelation.OTHER and body.mime_path not in grouped_paths:
            groups.append(_singleton(body))

    return _ordered(bodies, groups)


def _singleton(body: BodyPart) -> BodyGroup:
    return BodyGroup(
        group_key=f"standalone:{body.mime_path}",
        alternative_path=None,
        versions=[_version_ref(body)],
        compared=False,
    )


def _version_ref(body: BodyPart) -> BodyVersionRef:
    return BodyVersionRef(
        mime_path=body.mime_path,
        content_type=body.content_type,
        relation=body.alt_relation,
        text_source=body.text_source,
    )


def _comparison_pair(members: list[BodyPart]) -> tuple[BodyPart | None, BodyPart | None]:
    """Pick the canonical pair to diff: first plain vs first html.

    If the group contains no plain/html pair but has two readable versions,
    compare the first two (direction-neutral labels are used). A lone version
    is returned as ``(None, None)`` — nothing is compared.
    """
    plain = next((b for b in members if b.alt_relation is AltRelation.PLAIN), None)
    html = next((b for b in members if b.alt_relation is AltRelation.HTML), None)
    if plain is not None and html is not None:
        return plain, html
    if len(members) >= 2:
        return members[0], members[1]
    return None, None


def _ordered(bodies: list[BodyPart], groups: list[BodyGroup]) -> list[BodyGroup]:
    order = {b.mime_path: i for i, b in enumerate(bodies)}

    def first_index(group: BodyGroup) -> int:
        return min(order.get(v.mime_path, len(order)) for v in group.versions)

    return sorted(groups, key=first_index)


def _path_sort_key(path: str) -> tuple[int, ...]:
    # "1.3.1" -> (1, 3, 1); embedded ".m1" segments sort after numeric siblings.
    parts = path.split(".")
    key: list[int] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        elif part.startswith("m") and part[1:].isdigit():
            key.append(10_000 + int(part[1:]))
        else:
            key.append(100_000)
    return tuple(key)


# -- Paragraph diff ---------------------------------------------------------


def compare_versions(left: BodyPart, right: BodyPart) -> list[ParagraphDiff]:
    """Align two readable versions paragraph by paragraph.

    Returns locatable rows tagged ``equal`` / ``replaced`` / ``*_only``.
    Side-specific labels (``plain_only`` / ``html_only``) are used only when
    the two sides really are a plain and an HTML version.
    """
    left_paras = split_paragraphs(readable_text(left))
    right_paras = split_paragraphs(readable_text(right))
    plain_html_pair = left.alt_relation is AltRelation.PLAIN and right.alt_relation is AltRelation.HTML

    def only_left(text: str) -> ParagraphDiff:
        kind = "plain_only" if plain_html_pair else "left_only"
        return ParagraphDiff(kind, left.mime_path, None, text, None)

    def only_right(text: str) -> ParagraphDiff:
        kind = "html_only" if plain_html_pair else "right_only"
        return ParagraphDiff(kind, None, right.mime_path, None, text)

    rows: list[ParagraphDiff] = []
    matcher = SequenceMatcher(None, left_paras, right_paras, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_chunk = left_paras[i1:i2]
        right_chunk = right_paras[j1:j2]
        if tag == "equal":
            for text in left_chunk:
                rows.append(ParagraphDiff("equal", left.mime_path, right.mime_path, text, text))
        elif tag == "replace":
            # Do not blindly pair positionally: two unrelated paragraphs must
            # not be called a "replacement". Pair only rows that share real
            # content tokens; every leftover is reported one-sided.
            rows.extend(_align_replaced(left_chunk, right_chunk, left.mime_path,
                                        right.mime_path, plain_html_pair))
        elif tag == "delete":
            rows.extend(only_left(t) for t in left_chunk)
        elif tag == "insert":
            rows.extend(only_right(t) for t in right_chunk)
    return rows


def _content_tokens(text: str) -> set[str]:
    """Significant comparison tokens: words of 3+ chars, case-folded."""
    return {tok for tok in re.findall(r"\w+", text.lower()) if len(tok) >= 3}


def _align_replaced(
    left_chunk: list[str],
    right_chunk: list[str],
    left_path: str,
    right_path: str,
    plain_html_pair: bool,
) -> list[ParagraphDiff]:
    """Pair genuinely related paragraphs inside a replace opcode.

    A pair is acknowledged only when both sides share at least one
    significant token AND are reasonably similar (SequenceMatcher ratio).
    Everything else is emitted as plain_only/html_only — a relationship is
    never claimed for unrelated text.
    """
    only_left_kind = "plain_only" if plain_html_pair else "left_only"
    only_right_kind = "html_only" if plain_html_pair else "right_only"
    used_right: set[int] = set()
    rows: list[ParagraphDiff] = []
    for lp in left_chunk:
        tokens = _content_tokens(lp)
        best_j = -1
        best_ratio = 0.0
        for j, rp in enumerate(right_chunk):
            if j in used_right:
                continue
            shared = tokens & _content_tokens(rp)
            ratio = SequenceMatcher(None, lp, rp).ratio()
            if (shared and ratio >= 0.3) or ratio >= 0.72:
                if ratio > best_ratio:
                    best_ratio, best_j = ratio, j
        if best_j >= 0:
            used_right.add(best_j)
            rows.append(ParagraphDiff("replaced", left_path, right_path, lp, right_chunk[best_j]))
        else:
            rows.append(ParagraphDiff(only_left_kind, left_path, None, lp, None))
    for j, rp in enumerate(right_chunk):
        if j not in used_right:
            rows.append(ParagraphDiff(only_right_kind, None, right_path, None, rp))
    return rows


def diff_summary(diffs: Iterable[ParagraphDiff | dict]) -> dict[str, int]:
    """Compact counters for API summaries (accepts dataclasses or dicts)."""
    counts = {"equal": 0, "replaced": 0, "plain_only": 0, "html_only": 0,
              "left_only": 0, "right_only": 0}
    for row in diffs:
        kind = row.kind if not isinstance(row, dict) else row.get("kind")
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts
