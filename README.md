# EML Archive

Enterprise mail-fact service: it parses **EML** files into structured,
searchable records. FastAPI + Python's **`email` standard library** for
parsing, **PostgreSQL** for headers/relationships/body facts, and a
**controlled local directory** for raw messages and attachment bytes.

There is **no frontend** — JSON HTTP API only.

## Security model

| Concern | How it is handled |
|---|---|
| Attachment path escape | Storage root is resolved once; sender-supplied names are reduced to one safe basename (`app/storage.py::safe_basename`) and bytes are written to **content-addressed** shard names. Every path is re-validated with `Path.relative_to(root)`. Downloads re-validate the stored path and serve a sanitized `Content-Disposition` filename. |
| HTML / scripts / remote resources | HTML is stored only as: sanitized allow-list HTML (`safe_html`), fully escaped HTML (`escaped_html`), and extracted plain text. The stdlib allow-list sanitizer (`app/parser/html_sanitizer.py`) removes `<script>`, `<style>`, `<iframe>`, event handlers, `style=`/`class=`/`data=`, and **every** loading URL except inline `cid:` resources. Remote `http(s)`, protocol-relative, `data:`, `vbscript:` and `javascript:` loaders are stripped. |
| Body versions / search snippets | The plain-text and HTML alternatives are parsed as independently-located parts; search and diffs only ever use the plain-part text or the **html-derived** extracted text. Snippets are plain text, entity-escaped and truncated — raw or sanitized HTML is never rendered into one. Versions are only paired when they really share a `multipart/alternative` ancestor, and legacy records without recoverable parts report `unknown` ("来源未知") rather than inventing a relationship. |
| Inline resources | `multipart/related` / `Content-ID` parts are parsed as binary attachments with `content_id`; HTML parts record `referenced_cids` — links are facts, nothing is fetched. |
| Attachment bytes in logs | Only metadata is logged (content type, size, sha256, relative path). A test asserts payload markers never appear in log records. |
| Upload size | Hard streaming cap (`EMLARCH_MAX_UPLOAD_BYTES`) before persistence. |
| SQL | psycopg3 parameterized statements throughout; no string-built DML. |
| File modes | Stored files default to `0600`. |

## Parsing behavior

* **Multi-layer MIME**: a full structural tree (`multipart/mixed → alternative → related`, `message/rfc822` forwarded messages recursed) with stable MIME paths (`1.2`, `1.3.1`, `2.m1`).
* **Charsets**: declared charset is used when valid; on failure a recorded
  fallback ladder runs (`utf-8 → gb18030 → big5 → shift_jis → iso-8859-1`).
  Every substitution is a defect with its MIME stage.
* **Attachment names**: RFC2047/RFC2231 decoded display name kept **and** the
  raw encoded header preserved for forensics.
* **Defects are locatable**: boundary mismatches, unknown charsets, invalid
  dates, malformed Message-IDs, bad CTEs are collected per stage rather than
  raised. Results are `ok` / `defective` / `failed`; even `failed` uploads get
  an ingest row with the raw digest and defects.
* **Provenance**: each ingest stores the raw EML sha256, size, on-disk path
  and the parsed result references the same digest.

## Body version reconciliation

The `text/plain` and `text/html` alternatives of one email often disagree.
Implemented in `app/body_compare.py` (pure) + `app/body_facts.py`
(serialization/search snippets):

* **Parts stay separate and locatable**: every readable part keeps its MIME
  path, content type, `text_source` (`plain_part` for a text/plain part's raw
  text, `html_derived` for text extracted from a text/html part) and the MIME
  path of its enclosing `multipart/alternative`.
* **No fabricated correspondence**: versions are grouped only when they share
  a real `multipart/alternative` ancestor (nested alternatives start their own
  group). Parts outside an alternative container are explicit singleton
  groups; parts inside an embedded `message/rfc822` are never paired with the
  outer body.
* **Paragraph diffs are explainable**: versions are compared on their
  *readable text* (never raw HTML) and aligned paragraph by paragraph
  (`difflib`). Rows are `equal` / `replaced` / `plain_only` / `html_only`
  (plus direction-neutral `left_only` / `right_only`), each carrying the MIME
  path it came from. Two unrelated paragraphs are reported one-sided rather
  than called a "replacement"; the group status is
  `identical` / `divergent` / `unpaired`.
* **Search returns where it hit**: `/search` yields **one result per
  message**; `body_matches` lists every version containing the word with its
  MIME path, content type, source and an escaped, single-line plain-text
  snippet. A word that exists only in the HTML therefore locates the
  `html_derived` version; a word in both lists two sources on the same single
  result.
* **Scripts never run or leak**: search/diff/snippet text comes exclusively
  from the script-free extractor; `<script>` payload words are not even
  searchable. The detail view exposes `safe_html` (allow-list) and
  `escaped_html` (fully escaped) but never places raw HTML into a snippet.
* **Legacy mail**: records lacking the new reconciliation are reconstructed
  when the original part facts (MIME paths, content types, tree) survive
  (`provenance: reconstructed`). When the original parts cannot be obtained at
  all, each version is reported with `text_source: unknown` /
  `status: source_unknown` ("来源未知"), `compared: false`, and no diff. The
  PostgreSQL schema upgrades in place (`ALTER TABLE ... IF NOT EXISTS`);
  backfilled columns default to `unknown`.

## Threading / conversations

Implemented in `app/threads.py` (pure function, unit tested):

* Edges come **only** from `Message-ID`, `References`, `In-Reply-To`.
* Same/normalized subjects are **weak candidates** — surfaced as
  `weak_suggestions` (with a recency window), **never auto-merged**.
* **Duplicate** Message-IDs are retained as a conflict (`duplicate_ids`) —
  messages are not merged into one row; the `messages.message_id` column is
  deliberately not unique.
* **Missing** Message-ID rows are flagged `missing_id` and only join a thread
  through reference tokens.
* Reference cycles are detected with bounded three-color DFS (`cycles`);
  dangling references (`dangling_references`) are reported, not hidden.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/ingest` | multipart upload of one `.eml`; returns status, digest, parts, attachments, threading report |
| GET | `/messages` / `/messages/{id}` | list / full detail (tree, body versions + paragraph diffs, attachments, defects) |
| GET | `/messages/{id}/attachments/{aid}/download` | stream attachment bytes (path re-validated) |
| GET | `/search?q=` | one result per message over subject, Message-ID, headers and readable body text; per-version hits (`mime_path`, `text_source`, escaped snippet) |
| GET | `/threads` / `/threads/{key}` | thread summaries / ordered members with reference headers |
| POST | `/threads/rebuild` | recompute all threads; returns conflicts/cycles/dangling/weak hints |
| GET | `/ingests/{id}` | provenance: raw digest/path + every defect located by stage |
| GET | `/failures` | failed/defective ingests with their defect lists |
| GET | `/health` | backend and configured storage roots |

## Running

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

export EMLARCH_DATABASE_DSN="postgresql://user:pass@localhost:5432/emlarch"
export EMLARCH_ATTACHMENT_DIR=/var/lib/emlarchive/attachments
export EMLARCH_RAW_DIR=/var/lib/emlarchive/raw
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The schema is applied automatically on startup (`app/sql/schema.sql`).
Without a DSN the service boots an in-memory repository (useful for demos).

### Generate boundary samples

```bash
.venv/bin/python scripts/generate_samples.py
ls samples/
# 01 multibyte/nested, 02 circular refs, 03 missing id + bogus charset,
# 04 duplicate ids, 05 same subject different threads, 06 corrupt boundary,
# 07 bad CTE, 08 path traversal attachment, 09 XSS/remote HTML,
# 10 divergent plain/html alternatives
```

### Tests

```bash
.venv/bin/python -m pytest                       # unit + API tests (memory backend)
EMLARCH_RUN_PG_TESTS=1 EMLARCH_TEST_DSN='postgresql://postgres@/postgres?host=/tmp/pgsock&port=55432' \
  .venv/bin/python -m pytest                     # + real PostgreSQL integration tests
```

### Quick manual check

```bash
curl -F "file=@samples/01_multibyte.eml" http://127.0.0.1:8080/ingest
curl "http://127.0.0.1:8080/search?q=GB18030"
```

## Layout

```
app/
  config.py            env-driven configuration (roots, cap, DSN)
  parser/
    eml_parser.py      stdlib email parsing, structural walk, charset ladder
    html_sanitizer.py  allow-list sanitizer + escaping + text extraction
    models.py          structured result dataclasses
  body_compare.py      alternative grouping by real MIME nesting + paragraph diffs
  body_facts.py        provenance JSON, legacy reconstruction, safe snippets/search
  storage.py           ControlledStorage (path safety, 0600, metadata logs)
  threads.py           Message-ID graph + cycles + conflicts + weak subjects
  pg_repository.py     PostgreSQL persistence (psycopg3)
  memory_repository.py same interface, in-memory (tests / demo)
  service.py           parse -> store -> persist -> thread orchestration
  main.py / schemas.py FastAPI app and response models
  sql/schema.sql       DDL (ids intentionally non-unique; in-place upgrades)
scripts/generate_samples.py
samples/               generated edge-case EML files
tests/                 unit, API and PostgreSQL integration tests
```
