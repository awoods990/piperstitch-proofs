"""The security headers this service went to production without.

The framing rule is the one with teeth: a proof page carries an Approve
button, and an approval is the product. A page that can be dropped into
an invisible iframe can have that button clicked by someone who believed
they were clicking something else.
"""

from fastapi.testclient import TestClient

from app import config
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_every_response_refuses_to_be_framed():
    r = _client().get("/health")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_the_rest_of_the_baseline_headers_are_present():
    h = _client().get("/health").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp


def test_headers_reach_redirects_and_errors_too_not_just_200s():
    # The root redirects and an unknown path 404s; a middleware that only
    # decorated successful responses would leave both bare.
    for path, expected in (("/", 303), ("/no-such-page", 404)):
        r = _client().get(path, follow_redirects=False)
        assert r.status_code == expected
        assert r.headers["X-Frame-Options"] == "DENY", f"{path} came back without the framing header"


def test_hsts_tracks_whether_we_are_actually_served_over_https(monkeypatch):
    # Local development is http and must not be pinned to https by a header
    # the developer cannot then undo in their browser.
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://127.0.0.1:8100")
    assert "Strict-Transport-Security" not in _client().get("/health").headers
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://proofs.piperstitch.com")
    assert _client().get("/health").headers["Strict-Transport-Security"] == "max-age=15552000; includeSubDomains"
