# Import order matters: ``models`` and ``html_sanitizer`` are dependency-free,
# while ``eml_parser`` (via app.body_compare) imports ``app.parser.models``.
# Loading the leaf modules first keeps that import cycle resolvable.
from .models import (
    Address,
    AltRelation,
    Attachment,
    BodyGroup,
    BodyPart,
    BodyVersionRef,
    ContentDisposition,
    Defect,
    ParagraphDiff,
    ParseStatus,
    PartNode,
    ParsedMessage,
    TextSource,
)
from .html_sanitizer import escape_html, sanitize_html, strip_to_text
from .eml_parser import ParseFailure, parse_eml

__all__ = [
    "parse_eml",
    "ParseFailure",
    "sanitize_html",
    "escape_html",
    "strip_to_text",
    "ParsedMessage",
    "BodyPart",
    "BodyGroup",
    "BodyVersionRef",
    "ParagraphDiff",
    "Attachment",
    "Address",
    "Defect",
    "PartNode",
    "ParseStatus",
    "ContentDisposition",
    "TextSource",
    "AltRelation",
]
