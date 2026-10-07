"""Structured result types produced by the EML parser.

The parser only *describes* a message; persistence, sanitization policy and
threading live in other modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ParseStatus(str, Enum):
    OK = "ok"
    DEFECTIVE = "defective"  # parsed, but the email library reported defects
    FAILED = "failed"  # could not produce even a partial structure


class ContentDisposition(str, Enum):
    INLINE = "inline"
    ATTACHMENT = "attachment"
    UNKNOWN = "unknown"


class TextSource(str, Enum):
    """Where the searchable text of a body part came from.

    ``PLAIN_PART``  : the raw text of a text/plain MIME part.
    ``HTML_DERIVED``: text extracted/sanitized from a text/html MIME part;
                      never the raw HTML itself.
    ``UNKNOWN``     : reconstructed from storage that predates provenance
                      tracking; the originating part cannot be established.
    """

    PLAIN_PART = "plain_part"
    HTML_DERIVED = "html_derived"
    UNKNOWN = "unknown"


class AltRelation(str, Enum):
    """How a body version relates to its alternative group."""

    PLAIN = "text_plain"
    HTML = "text_html"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Address:
    display_name: str
    address: str
    raw: str


@dataclass(frozen=True)
class Defect:
    """One parser defect (boundary mismatch, bad encoding, ...).

    ``stage`` is where it was observed so failures can be located back to a
    specific MIME part or processing step.
    """

    stage: str
    level: str  # defect class name, e.g. StartBoundaryNotFoundDefect
    message: str


@dataclass
class BodyPart:
    """A displayable text part (text/plain or text/html)."""

    mime_path: str  # e.g. "1.2" — location inside the MIME tree
    content_type: str  # e.g. "text/html"
    charset: str | None
    declared_charset: str | None  # what the header said, before substitution
    disposition: ContentDisposition
    content_id: str | None
    content_location: str | None
    byte_size: int
    text: str
    # html variants are None for text/plain; see sanitize_html()
    safe_html: str | None = None
    plain_text: str | None = None
    # Inline resource cids this html part references (via cid: URIs)
    referenced_cids: list[str] = field(default_factory=list)
    # Provenance of the searchable text for this part.
    text_source: TextSource = TextSource.UNKNOWN
    # MIME path of the enclosing multipart/alternative ("" / "0" when none).
    alternative_path: str | None = None
    # Version role inside its alternative group.
    alt_relation: AltRelation = AltRelation.UNKNOWN


@dataclass(frozen=True)
class BodyVersionRef:
    """One readable version referenced by a :class:`BodyGroup`.

    ``search_text`` is the text used for search/diff: the decoded part text
    for text/plain and the script-free extraction for text/html. Raw HTML is
    never carried here.
    """

    mime_path: str
    content_type: str
    relation: AltRelation
    text_source: TextSource


@dataclass
class BodyGroup:
    """Readable versions that represent the *same* logical message body.

    Only parts physically nested in the same ``multipart/alternative``
    container are grouped. Parts outside any alternative container form
    singleton groups, and versions are never paired across unrelated parts.
    """

    group_key: str  # mime path of the alternative container; "standalone:<path>"
    alternative_path: str | None  # None for a standalone body
    versions: list[BodyVersionRef] = field(default_factory=list)
    # Paragraph-level, human-readable comparison of the readable versions.
    paragraph_diffs: list["ParagraphDiff"] = field(default_factory=list)
    compared: bool = False  # False when no comparable pair exists


@dataclass(frozen=True)
class ParagraphDiff:
    """One aligned row of the paragraph-level version comparison.

    ``kind`` is one of:
      * ``equal``     : same paragraph appears in both compared versions.
      * ``replaced``  : paragraphs differ; left/right show each wording.
      * ``plain_only``: paragraph present only in the text/plain version.
      * ``html_only`` : paragraph present only in the HTML-derived version.
      * ``left_only`` / ``right_only``: direction-neutral labels for
        non plain/html pairings.
    Each side carries the mime path it came from, so every claim is locatable.
    """

    kind: str
    left_path: str | None
    right_path: str | None
    left_text: str | None
    right_text: str | None


@dataclass
class Attachment:
    """A non-displayable part saved to the controlled directory."""

    mime_path: str
    content_type: str
    charset: str | None
    disposition: ContentDisposition
    filename: str | None  # decoded, sanitized display name (may be None)
    raw_filename: str | None  # exactly as encoded in the header
    content_id: str | None
    content_location: str | None
    byte_size: int
    checksum_sha256: str
    # Filled in by the storage layer; parser leaves both as None.
    storage_path: str | None = None  # path relative to the controlled root
    bytes_to_persist: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Header:
    name: str
    value: str  # RFC2047-decoded
    raw_value: str  # original encoded text
    ordinal: int


@dataclass
class PartNode:
    """Structural node of the (possibly nested / message/rfc822) MIME tree."""

    mime_path: str
    content_type: str
    disposition: ContentDisposition
    filename: str | None
    is_multipart: bool
    is_embedded_message: bool
    byte_size: int
    children: list["PartNode"] = field(default_factory=list)


@dataclass
class ParsedMessage:
    # Identity
    message_id: str | None
    references: list[str]
    in_reply_to: list[str]
    subject: str | None
    raw_subject: str | None
    date: datetime | None
    from_: list[Address]
    to: list[Address]
    cc: list[Address]
    bcc: list[Address]
    reply_to: list[Address]
    sender: list[Address]
    headers: list[Header]
    # Structure
    tree: PartNode | None
    bodies: list[BodyPart]
    attachments: list[Attachment]
    # Provenance / quality
    defects: list[Defect]
    status: ParseStatus
    fatal_error: str | None = None
    # Readable-body version groups (multipart/alternative) with paragraph diffs.
    body_groups: list[BodyGroup] = field(default_factory=list)
    # Filled by caller (parser operates on bytes only)
    raw_sha256: str | None = None
    raw_size: int | None = None
