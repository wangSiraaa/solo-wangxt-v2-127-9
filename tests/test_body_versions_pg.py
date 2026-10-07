"""Body-version reconciliation against the real PostgreSQL backend.

Mirrors the memory-backend acceptance cases so the schema columns, JSONB
reconciliation storage and ILIKE search all behave identically to the
in-memory repository.
"""
import pytest

from app.config import Settings
from app.main import create_app
from conftest import SAMPLES

pytestmark = pytest.mark.pg


def _post(c, name):
    return c.post(
        "/ingest",
        files={"file": (name, (SAMPLES / name).read_bytes(), "message/rfc822")},
    )


def test_html_only_word_locates_html_version_pg(pg_client):
    c, _ = pg_client
    r = _post(c, "10_alt_divergent.eml")
    assert r.status_code == 201, r.text
    pk = r.json()["message_pk"]

    only = c.get("/search", params={"q": "ZEPPELINUNIQUE"}).json()
    assert only["count"] == 1 and only["results"][0]["id"] == pk
    hit = only["results"][0]["body_matches"][0]
    assert hit["mime_path"] == "2"
    assert hit["content_type"] == "text/html"
    assert hit["text_source"] == "html_derived"
    assert "<script" not in hit["snippet"]


def test_shared_word_one_message_two_sources_pg(pg_client):
    c, _ = pg_client
    _post(c, "10_alt_divergent.eml")
    res = c.get("/search", params={"q": "quarterly"}).json()
    assert res["count"] == 1
    sources = {(h["mime_path"], h["text_source"]) for h in res["results"][0]["body_matches"]}
    assert sources == {("1", "plain_part"), ("2", "html_derived")}


def test_detail_versions_and_diffs_pg(pg_client):
    c, _ = pg_client
    pk = _post(c, "10_alt_divergent.eml").json()["message_pk"]
    detail = c.get(f"/messages/{pk}").json()
    assert len(detail["body_groups"]) == 1
    group = detail["body_groups"][0]
    assert group["provenance"] == "stored" and group["status"] == "divergent"
    assert {v["mime_path"] for v in group["versions"]} == {"1", "2"}
    assert {d["kind"] for d in group["paragraph_diffs"]} >= {
        "equal", "replaced", "plain_only", "html_only"
    }
    # Provenance columns persisted per body row.
    by_path = {b["mime_path"]: b for b in detail["bodies"]}
    assert by_path["1"]["text_source"] == "plain_part"
    assert by_path["2"]["text_source"] == "html_derived"
    assert by_path["1"]["alternative_path"] == "0"


def test_script_payload_not_searchable_pg(pg_client):
    c, _ = pg_client
    _post(c, "09_html_xss.eml")
    for payload in ("document.cookie", "fetch(", "alert(1)"):
        assert c.get("/search", params={"q": payload}).json()["count"] == 0
    hit = c.get("/search", params={"q": "hello"}).json()["results"][0]["body_matches"][0]
    assert "fetch" not in hit["snippet"] and "<script" not in hit["snippet"]


def test_legacy_rows_upgrade_with_unknown_provenance_pg(pg_client):
    """A database created before the feature must keep reading its old rows.

    Simulates a legacy row by nulling the reconciliation JSONB and the new
    per-part columns (backfilled with the 'unknown' default by the schema
    upgrade); with the MIME tree also gone, the API must report
    source_unknown rather than inventing a pairing.
    """
    c, arch = pg_client
    pk = _post(c, "10_alt_divergent.eml").json()["message_pk"]
    import psycopg

    with psycopg.connect(arch.settings.database_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET body_groups = NULL, tree_json = 'null'::jsonb WHERE id = %s",
            (pk,),
        )
        cur.execute(
            "UPDATE bodies SET text_source='unknown', alt_relation='unknown', "
            "alternative_path=NULL WHERE message_pk = %s",
            (pk,),
        )
        conn.commit()

    detail = c.get(f"/messages/{pk}").json()
    assert detail["body_groups"]
    for group in detail["body_groups"]:
        assert group["provenance"] == "unknown"
        assert group["status"] == "source_unknown"
        assert group["compared"] is False
        assert all(v["text_source"] == "unknown" for v in group["versions"])


def test_schema_upgrade_is_idempotent_on_legacy_database_pg(pg_client):
    c, arch = pg_client
    # Re-running init_schema on the live database exercises ALTER ... IF NOT
    # EXISTS against columns already present; it must not error.
    arch.repo.init_schema()
    assert c.get("/health").status_code == 200


def test_upgrade_from_pre_feature_schema_pg(pg_dsn):
    """A database created with the old DDL upgrades cleanly and keeps serving.

    Build the pre-feature messages/bodies tables (no provenance columns, no
    body_groups), insert a legacy row, then point a repository at it: schema
    init must ALTER the tables in place and the old row must read back with
    unknown provenance rather than crashing or being fabricated.
    """
    import psycopg

    from app.pg_repository import PgRepository
    from fastapi.testclient import TestClient

    with psycopg.connect(pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE ingests (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                source_name TEXT, status TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                raw_size BIGINT NOT NULL, raw_path TEXT, fatal_error TEXT,
                defect_count INT NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                ingest_id BIGINT NOT NULL REFERENCES ingests(id) ON DELETE CASCADE,
                message_id TEXT, subject TEXT, raw_subject TEXT, date TIMESTAMPTZ,
                from_json JSONB NOT NULL DEFAULT '[]', to_json JSONB NOT NULL DEFAULT '[]',
                cc_json JSONB NOT NULL DEFAULT '[]', bcc_json JSONB NOT NULL DEFAULT '[]',
                reply_to_json JSONB NOT NULL DEFAULT '[]', sender_json JSONB NOT NULL DEFAULT '[]',
                tree_json JSONB NOT NULL, raw_sha256 TEXT NOT NULL, raw_path TEXT,
                thread_key TEXT, missing_id BOOLEAN NOT NULL DEFAULT FALSE,
                search_tsv TSVECTOR, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE bodies (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                message_pk BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                mime_path TEXT NOT NULL, content_type TEXT NOT NULL, charset TEXT,
                declared_charset TEXT, disposition TEXT NOT NULL, content_id TEXT,
                content_location TEXT, byte_size BIGINT NOT NULL, text TEXT,
                safe_html TEXT, escaped_html TEXT, plain_text TEXT,
                referenced_cids JSONB NOT NULL DEFAULT '[]'
            );
            CREATE TABLE attachments (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                message_pk BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                mime_path TEXT NOT NULL, content_type TEXT NOT NULL, charset TEXT,
                disposition TEXT NOT NULL, filename TEXT, raw_filename TEXT,
                content_id TEXT, content_location TEXT, byte_size BIGINT NOT NULL,
                checksum_sha256 TEXT NOT NULL, storage_path TEXT, stored BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE defects (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                ingest_id BIGINT NOT NULL REFERENCES ingests(id) ON DELETE CASCADE,
                message_pk BIGINT REFERENCES messages(id) ON DELETE CASCADE,
                stage TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
            );
            CREATE TABLE message_headers (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                ordinal INT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, raw_value TEXT NOT NULL
            );
            CREATE TABLE message_identifiers (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                message_pk BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, value TEXT NOT NULL, ordinal INT NOT NULL
            );
            CREATE TABLE thread_runs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                ran_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                duplicate_ids JSONB NOT NULL DEFAULT '{}', dangling JSONB NOT NULL DEFAULT '[]',
                cycles JSONB NOT NULL DEFAULT '[]', weak_suggestions JSONB NOT NULL DEFAULT '[]'
            );
            """
        )
        cur.execute(
            "INSERT INTO ingests (status, raw_sha256, raw_size) VALUES ('ok', %s, 0)",
            ("legacy-sha",),
        )
        legacy_tree = {
            "mime_path": "0",
            "content_type": "multipart/mixed",
            "disposition": "unknown",
            "filename": None,
            "is_multipart": True,
            "is_embedded_message": False,
            "byte_size": 0,
            "children": [
                {
                    "mime_path": "1",
                    "content_type": "text/plain",
                    "disposition": "unknown",
                    "filename": None,
                    "is_multipart": False,
                    "is_embedded_message": False,
                    "byte_size": 5,
                    "children": [],
                }
            ],
        }
        from psycopg.types.json import Jsonb

        cur.execute(
            """
            INSERT INTO messages (ingest_id, message_id, subject, tree_json, raw_sha256)
            VALUES (1, 'legacy@example.com', 'legacy subject', %s, 'legacy-sha')
            RETURNING id
            """,
            (Jsonb(legacy_tree),),
        )
        msg_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO bodies (message_pk, mime_path, content_type, disposition, "
            "byte_size, plain_text) VALUES (%s,'1','text/plain','inline',5,'legacy only word')",
            (msg_pk,),
        )

    # New repository against the old schema: in-place upgrade must succeed.
    repo = PgRepository(pg_dsn)
    repo.init_schema()

    import tempfile
    from pathlib import Path

    settings = Settings(
        database_dsn=pg_dsn,
        attachment_dir=Path(tempfile.mkdtemp()),
        raw_dir=Path(tempfile.mkdtemp()),
        max_upload_bytes=10 * 1024 * 1024,
        file_mode=0o600,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        # The stored provenance column was backfilled with 'unknown'...
        import psycopg

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT text_source, alt_relation, alternative_path FROM bodies WHERE id = 1"
            )
            stored_source, stored_relation, stored_alt = cur.fetchone()
        assert stored_source == "unknown"
        assert stored_relation == "unknown"
        assert stored_alt is None
        # ...but because the original part facts (content type, mime path and
        # the MIME tree) survived, the grouping/diff is honestly reconstructed
        # (a singleton plain body here), not fabricated.
        detail = c.get(f"/messages/{msg_pk}").json()
        assert len(detail["body_groups"]) == 1
        group = detail["body_groups"][0]
        assert group["provenance"] == "reconstructed"
        assert group["status"] == "unpaired"
        assert group["versions"][0]["text_source"] == "plain_part"
        # Search over legacy extracted text still works after the upgrade.
        hits = c.get("/search", params={"q": "legacy only word"}).json()
        assert hits["count"] == 1

        # Once the structural facts are gone too, the answer must be
        # "来源未知" (unknown) — never a guessed correspondence.
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET tree_json = 'null'::jsonb WHERE id = %s",
                (msg_pk,),
            )
            conn.commit()
        detail2 = c.get(f"/messages/{msg_pk}").json()
        assert all(g["provenance"] == "unknown" for g in detail2["body_groups"])
        assert all(g["compared"] is False for g in detail2["body_groups"])
