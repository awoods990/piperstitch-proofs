"""Everything Proofs asks PiperStitch Core for, behind one small client
so tests can swap in fixtures (PRD v1.1 changes 1, 5 and 6).

Two Core services:

* **License Admin** (`config.LICENSE_ADMIN_URL`, `WEB_API_KEY`): who the shop
  is and what it is entitled to, and the shop's saved projects. The same
  `/api/web/*` routes Core's own web app uses.
* **The app server** (`config.CORE_SERVER_URL`): the stitch engine.
  `digitize` returns the plan (one row per command), the colour sequence
  and statistics; `export` returns machine-file bytes. In production the
  key-guarded `/api/v1/internal/*` routes are used; locally, with Core's
  server run without accounts, the open routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config

log = logging.getLogger("proofs.core")

MACHINE_FORMATS = ("dst", "pes", "exp", "jef", "vp3")


class CoreError(Exception):
    pass


@dataclass
class CoreSession:
    customer_id: int
    email: str
    name: str
    status: str
    entitled: bool


class LicenseAdminClient:
    def __init__(self, base_url: str = "", api_key: str = ""):
        self.base_url = (base_url or config.LICENSE_ADMIN_URL).rstrip("/")
        self.api_key = api_key or config.WEB_API_KEY

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _post(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        for name in ("WEB_API_KEY", "LICENSE_ADMIN_URL"):
            if config.bad_ascii(name):
                raise CoreError("Configuration problem: " + config.bad_ascii(name))
        try:
            r = httpx.post(f"{self.base_url}{path}", json=body, params=params, headers={"X-API-Key": self.api_key}, timeout=30)
        except httpx.HTTPError as e:
            raise CoreError(f"License Admin is unreachable: {e}") from e
        if r.status_code >= 500:
            raise CoreError(f"License Admin error {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise CoreError(f"License Admin answered {r.status_code} with something that isn't JSON from {self.base_url}{path} -- is LICENSE_ADMIN_URL right?")
        if r.status_code >= 400:
            raise CoreError(str(data.get("message") or data.get("error") or f"License Admin error {r.status_code}"))
        if not isinstance(data, dict):
            raise CoreError("Unexpected reply from License Admin")
        return data

    def request_signin(self, email: str) -> None:
        self._post("/api/web/signin/request", {"email": email})

    def verify_signin(self, email: str, code: str) -> str:
        """Returns the web session token."""
        return self._post("/api/web/signin/verify", {"email": email, "code": code})["token"]

    def session_state(self, token: str) -> CoreSession:
        s = self._post("/api/web/session", {"token": token})
        return CoreSession(customer_id=int(s["customer_id"]), email=s["email"], name=s.get("name") or "", status=s.get("status") or "", entitled=bool(s.get("entitled")))

    def list_projects(self, token: str) -> list[dict]:
        return self._post("/api/web/projects/list", {"token": token})["projects"]

    def get_project(self, token: str, project_id: str) -> dict:
        return self._post("/api/web/projects/get", {"token": token}, params={"id": project_id})


class StitchClient:
    def __init__(self, base_url: str = "", api_key: str = ""):
        self.base_url = (base_url or config.CORE_SERVER_URL).rstrip("/")
        self.api_key = api_key or config.CORE_API_KEY

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _prefix(self) -> str:
        return "/api/v1/internal" if self.api_key else "/api/v1"

    def digitize(self, document: dict, hoop_width_mm: Optional[float] = None, hoop_height_mm: Optional[float] = None) -> dict:
        for name in ("CORE_API_KEY", "CORE_SERVER_URL"):
            if config.bad_ascii(name):
                raise CoreError("Configuration problem: " + config.bad_ascii(name))
        body = {"document": document, "hoopWidthMM": hoop_width_mm, "hoopHeightMM": hoop_height_mm}
        try:
            r = httpx.post(f"{self.base_url}{self._prefix()}/digitize", json=body, headers=self._headers(), timeout=300)
        except httpx.HTTPError as e:
            raise CoreError(f"The stitch engine is unreachable: {e}") from e
        if r.status_code != 200:
            raise CoreError(f"The stitch engine returned {r.status_code}: {r.text[:200]}")
        return r.json()

    def export(self, document: dict, fmt: str) -> bytes:
        if fmt not in MACHINE_FORMATS:
            raise CoreError(f"Unknown machine format {fmt}")
        try:
            r = httpx.post(f"{self.base_url}{self._prefix()}/export/{fmt}", json={"document": document}, headers=self._headers(), timeout=300)
        except httpx.HTTPError as e:
            raise CoreError(f"The stitch engine is unreachable: {e}") from e
        if r.status_code != 200:
            raise CoreError(f"Export {fmt} failed with {r.status_code}: {r.text[:200]}")
        return r.content


# Module-level instances the app uses; tests replace them.
license_admin = LicenseAdminClient()
stitch = StitchClient()
