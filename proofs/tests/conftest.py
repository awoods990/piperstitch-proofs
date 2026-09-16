"""Test harness: a throwaway SQLite database and artifact directory per
session, an email outbox on disk, and a fake Core stitch client that
serves the captured fixtures (PRD v1.1 change 5: Core's repository is
never on Proofs' build path)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

_tmp = tempfile.mkdtemp(prefix="proofs-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmp) / "test.db")
os.environ["ARTIFACT_DIR"] = str(Path(_tmp) / "artifacts")
os.environ["EMAIL_OUTBOX_DIR"] = str(Path(_tmp) / "outbox")
os.environ["SMS_OUTBOX_DIR"] = str(Path(_tmp) / "sms")
os.environ["SESSION_SECRET"] = "test-secret-test-secret-test-secret-1234"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["LICENSE_ADMIN_URL"] = ""
os.environ["REQUIRE_LICENSE_ADMIN_SIGNIN"] = "false"
os.environ["CERTIFICATE_SIGNING_KEY"] = ""

from app import core_client, db as database  # noqa: E402  (after env)


class FakeStitchClient:
    """Serves the fixture digitize response and machine files for any
    document; a document whose name contains 'v2' gets a slightly
    different plan so version comparisons have something to see."""

    def __init__(self):
        self.digitized = json.loads((FIXTURES / "cap_digitize.json").read_text())
        self.files = {fmt: (FIXTURES / f"cap.{fmt}").read_bytes() for fmt in core_client.MACHINE_FORMATS}
        self.calls = 0
        # Which document object each colour block came from, so a document
        # with swapped thread colours (a colorway) reports the swapped
        # colours the way Core would.
        fixture_doc = json.loads((FIXTURES / "cap_document.json").read_text())
        def key(c):
            r = c["rgb"]
            return (r["r"], r["g"], r["b"])
        self.block_to_object = []
        for c in self.digitized["colors"]:
            idx = next((i for i, o in enumerate(fixture_doc["objects"]) if key(o["threadColor"]) == key(c)), None)
            self.block_to_object.append(idx)

    def digitize(self, document, hoop_width_mm=None, hoop_height_mm=None):
        self.calls += 1
        d = json.loads(json.dumps(self.digitized))
        objects = document.get("objects") or []
        for block, idx in enumerate(self.block_to_object):
            if idx is not None and idx < len(objects) and block < len(d["colors"]):
                d["colors"][block] = dict(objects[idx]["threadColor"])
        if "v2" in (document.get("name") or ""):
            # Drop the last 200 stitches: a genuinely different design.
            d["plan"]["commands"] = d["plan"]["commands"][:-200] + [[5, 0, 0]]
            d["stats"]["stitchCount"] -= 200
        return d

    def build_from_artwork(self, data, filename, *, name, width_mm, height_mm=None, fabric_type="standard", max_colors=None):
        self.built = getattr(self, "built", []) + [(filename, name, width_mm, fabric_type)]
        doc = json.loads((FIXTURES / "cap_document.json").read_text())
        doc["name"] = name
        return doc

    def export(self, document, fmt):
        data = self.files[fmt]
        if "v2" in (document.get("name") or ""):
            data = data[:-64] + bytes(64)
        return data


class FakeLicenseAdmin:
    """Stands in for License Admin's projects API: `configured` so the
    auto-digitize path runs; projects saved in memory."""
    configured = True
    base_url = "http://license-admin.test"
    api_key = "test"

    def __init__(self):
        self.projects: dict[str, dict] = {}

    def save_project(self, token, project_id, name, document):
        self.projects[project_id] = {"id": project_id, "name": name, "document": document, "widthMM": document.get("physicalWidthMM", 0), "heightMM": document.get("physicalHeightMM", 0)}
        return {"saved": True}

    def list_projects(self, token):
        return [{k: v for k, v in p.items() if k != "document"} for p in self.projects.values()]

    def get_project(self, token, project_id):
        return self.projects[project_id]

    def request_signin(self, email):
        raise AssertionError("tests use Proofs codes")

    def verify_signin(self, email, code):
        raise AssertionError("tests use Proofs codes")


@pytest.fixture(scope="session", autouse=True)
def _init():
    database.init_db()
    core_client.stitch = FakeStitchClient()
    core_client.license_admin = FakeLicenseAdmin()
    yield


@pytest.fixture
def document():
    return json.loads((FIXTURES / "cap_document.json").read_text())


@pytest.fixture
def outbox():
    p = Path(os.environ["EMAIL_OUTBOX_DIR"])
    p.mkdir(parents=True, exist_ok=True)

    class Outbox:
        def all(self):
            return [json.loads(f.read_text()) for f in sorted(p.glob("*.json"))]

        def clear(self):
            for f in p.glob("*.json"):
                f.unlink()

        def latest_to(self, email):
            for m in reversed(self.all()):
                if m["to"] == email:
                    return m
            return None

    box = Outbox()
    box.clear()
    return box
