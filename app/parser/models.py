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
    """Where the readable text of a body part came from.

    HTML parts are never indexed/snippet-rendered from raw markup; only text
    derived with the allow-list extractor is ever treated as readable text.
    ``UNKNOWN`` is reserved for legacy rows whose originating part/text kind
    can no longer be determined — correspondence must not be fabricated.
    """

    PLAIN = "plain"  # text/plain part, text is the decoded part body
    HTML_DERIVED = "html_derived"  # text/html part, text extracted from the HTML
    UNKNOWN = "unknown"  # legacy body row; originating part unavailable


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
    # Provenance of the readable text for this part. Plain parts carry the
    # decoded part body; HTML parts are only ever read via derived plain text.
    text_source: TextSource = TextSource.PLAIN
    # html variants are None for text/plain; see sanitize_html()
    safe_html: str | None = None
    plain_text: str | None = None
    # Inline resource cids this html part references (via cid: URIs)
    referenced_cids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BodyVersion:
    """One readable rendition of a message body, with full provenance.

    ``paragraphs``/``chars`` describe the readable text; the text itself is
    always *readable plain text only* (never raw HTML) wherever it is read.
    """

    mime_path: str | None  # MIME path of the originating part; None => legacy
    content_type: str | None  # "text/plain" / "text/html"; None => unknown
    text_source: TextSource
    source_label: str  # human-readable provenance label (may be 来源未知)
    charset: str | None = None
    paragraphs: int = 0  # number of readable paragraphs
    chars: int = 0
    known: bool = True  # False when originating parts are unavailable


@dataclass(frozen=True)
class ParagraphNote:
    """One explainable paragraph-level difference.

    ``paragraph`` is the normalized paragraph text, ``present_in`` lists the
    version keys (mime paths) that contain it. ``shared``/``only_in`` on the
    enclosing diff explain the cross-version relationship.
    """

    paragraph: str
    present_in: list[str]


@dataclass(frozen=True)
class BodyDiff:
    """Paragraph-level reconciliation of alternative body versions.

    Versions are grouped by their nearest ``multipart/alternative`` ancestor,
    which is the only relationship the MIME structure actually asserts. Parts
    with no alternative sibling stay in single-version groups — no
    correspondence is invented for them.
    """

    group_key: str  # mime path of the alternative container, or the part path
    alternative_path: str | None  # container MIME path; None => standalone
    relation: str  # "alternative" or "standalone"
    versions: list[BodyVersion]
    agreed: bool  # True when every version has the same paragraph set
    shared: list[str] = field(default_factory=list)  # paragraphs in all versions
    unique: list[ParagraphNote] = field(default_factory=list)  # only_in notes
    partial: list[ParagraphNote] = field(default_factory=list)  # subset of versions


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
    # Body version reconciliation (filled by the parser after the walk)
    body_versions: list[BodyVersion] = field(default_factory=list)
    body_diffs: list[BodyDiff] = field(default_factory=list)
    # Filled by caller (parser operates on bytes only)
    raw_sha256: str | None = None
    raw_size: int | None = None
