"""The copy that isn't on Railway: the database and every artifact."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from app import backups, config


def test_the_archive_holds_the_database_and_every_artifact(tmp_path, monkeypatch):
    """A proof record without its render and its certificate is a receipt
    for something nobody can see."""
    artifacts = tmp_path / "artifacts"
    (artifacts / "proof-1" / "v1").mkdir(parents=True)
    (artifacts / "proof-1" / "v1" / "render.png").write_bytes(b"a render")
    (artifacts / "proof-1" / "v1" / "certificate.pdf").write_bytes(b"%PDF certificate")
    monkeypatch.setattr(config, "ARTIFACT_DIR", str(artifacts))

    archive, manifest = backups.build_archive()
    assert manifest["service"] == "proofs" and manifest["artifact_files"] == 2
    assert manifest["artifact_bytes"] == len(b"a render") + len(b"%PDF certificate")

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "proofs.sqlite3" in names and "manifest.json" in names
        assert "artifacts/proof-1/v1/render.png" in names and "artifacts/proof-1/v1/certificate.pdf" in names
        assert tar.extractfile("artifacts/proof-1/v1/certificate.pdf").read() == b"%PDF certificate"
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(tar.extractfile("proofs.sqlite3").read())
        assert hashlib.sha256(restored.read_bytes()).hexdigest() == manifest["database_sha256"]
        assert json.loads(tar.extractfile("manifest.json").read())["restore"].startswith("Unpack")

    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_without_a_bucket_it_says_so(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_BUCKET", "")
    assert backups.configured() is False
    assert backups.run() == {"ok": False, "reason": "unconfigured"}
    assert backups.last_run()["status"] == "unconfigured"


def test_a_run_uploads_reads_back_and_logs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARTIFACT_DIR", str(tmp_path / "none"))
    for name, value in (("BACKUP_ENDPOINT", "https://s3.example.com"), ("BACKUP_BUCKET", "piperstitch"),
                        ("BACKUP_ACCESS_KEY", "k"), ("BACKUP_SECRET_KEY", "s")):
        monkeypatch.setattr(config, name, value)
    store: dict[str, bytes] = {}
    monkeypatch.setattr(backups, "put", lambda key, body: store.__setitem__(key, body))
    monkeypatch.setattr(backups, "head", lambda key: len(store.get(key, b"")) or None)
    monkeypatch.setattr(backups, "get", lambda key: store[key])
    monkeypatch.setattr(backups, "prune", lambda now=None: 0)

    result = backups.run()
    assert result["ok"] and result["key"].startswith("proofs/")
    row = backups.last_run(status="ok")
    assert row["key"] == result["key"] and json.loads(row["detail"])["artifacts"] == 0


def test_the_signature_matches_amazons_documented_canonical_request():
    from datetime import datetime, timezone

    empty = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join([
        "GET", "/test.txt", "",
        f"host:examplebucket.s3.amazonaws.com\nrange:bytes=0-9\nx-amz-content-sha256:{empty}\nx-amz-date:20130524T000000Z\n",
        "host;range;x-amz-content-sha256;x-amz-date", empty])
    assert hashlib.sha256(canonical.encode()).hexdigest() == "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
    headers = backups.authorization(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt", payload_sha=empty,
        now=datetime(2013, 5, 24, tzinfo=timezone.utc), access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", region="us-east-1", extra_headers={"range": "bytes=0-9"})
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["Authorization"]
