"""Upstream requests carry a Keycloak client-credentials token where the upstream requires one."""

import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.services import upstream

TOKEN_URL = "https://keycloak.test/realms/staff/protocol/openid-connect/token"


def settings(**overrides) -> Settings:
    values = {
        "upstream_farmer_base_url": "http://farmer-registry-dashboard-api.far",
        "upstream_a2c_base_url": "http://a2c.test",
        "oidc_token_url": TOKEN_URL,
        "oidc_client_id": "oan-reports-central",
        "oidc_client_secret": "s3cret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Recorder:
    """httpx transport: answers the token endpoint and the chart endpoints, and records every request."""

    def __init__(self, chart_status=200, token_status=200):
        self.requests: list[httpx.Request] = []
        self.chart_status = chart_status
        self.token_status = token_status
        self.tokens_issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            if self.token_status != 200:
                return httpx.Response(self.token_status, text="invalid_client")
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.tokens_issued}", "expires_in": 300})
        return httpx.Response(self.chart_status, json={"status": "ok", "data": [{"label": "a", "value": 1}]})

    def chart_requests(self):
        return [r for r in self.requests if str(r.url) != TOKEN_URL]


@pytest.fixture
def transport(monkeypatch):
    def install(recorder: Recorder):
        real = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(recorder)
            return real(*args, **kwargs)

        monkeypatch.setattr(upstream.httpx, "AsyncClient", client)
        return recorder

    return install


def fetch(service, s):
    return asyncio.run(upstream.fetch_chart(service=service, chart_key="farmerKpis", settings=s))


def test_registry_upstream_gets_bearer_token(transport):
    rec = transport(Recorder())
    data, meta = fetch("farmer", settings())
    assert meta["upstream_ok"], meta
    assert data == [{"label": "a", "value": 1}]
    token_request = rec.requests[0]
    assert str(token_request.url) == TOKEN_URL
    body = token_request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=oan-reports-central" in body
    assert rec.chart_requests()[0].headers["Authorization"] == "Bearer tok-1"


@pytest.fixture(autouse=True)
def fresh_token_cache():
    upstream._token_cache.update(key=None, token=None, expires_at=0.0)
    yield


def test_public_upstream_gets_no_token(transport):
    rec = transport(Recorder())
    _, meta = fetch("a2c", settings())
    assert meta["upstream_ok"], meta
    assert all(str(r.url) != TOKEN_URL for r in rec.requests)
    assert "Authorization" not in rec.chart_requests()[0].headers


def test_token_is_reused_until_expiry(transport):
    rec = transport(Recorder())
    s = settings()
    fetch("farmer", s)
    fetch("farmer", s)
    assert rec.tokens_issued == 1
    assert [r.headers["Authorization"] for r in rec.chart_requests()] == ["Bearer tok-1", "Bearer tok-1"]


def test_token_failure_is_reported_not_raised(transport):
    rec = transport(Recorder(token_status=401))
    data, meta = fetch("farmer", settings())
    assert data == [] and not meta["upstream_ok"]
    assert "token endpoint HTTP 401" in meta["error"]
    assert rec.chart_requests() == []


def test_no_token_url_means_no_token(transport):
    rec = transport(Recorder())
    _, meta = fetch("farmer", settings(oidc_token_url=""))
    assert meta["upstream_ok"], meta
    assert "Authorization" not in rec.chart_requests()[0].headers

