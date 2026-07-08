"""Drive federated search: the `files.list` query is restricted to useful
document types (Docs/Sheets/Slides, PDF, Office), never source code."""

from __future__ import annotations

import httpx
import pytest

from app.services.search import drive


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        return self._response


@pytest.mark.asyncio
async def test_search_query_restricts_to_useful_mime_types(monkeypatch):
    resp = httpx.Response(200, json={"files": []}, request=httpx.Request("GET", drive._BASE))
    fake = _FakeClient(resp)
    monkeypatch.setattr(drive.httpx, "AsyncClient", lambda *a, **kw: fake)

    await drive.DriveClient("tok").search("financial spreadsheet", limit=5)
    q = fake.calls[0]["params"]["q"]

    assert "fullText contains 'financial spreadsheet'" in q
    assert "trashed = false" in q
    # Useful document types are allowlisted...
    assert "mimeType = 'application/vnd.google-apps.spreadsheet'" in q
    assert "mimeType = 'application/vnd.google-apps.document'" in q
    assert "mimeType = 'application/pdf'" in q
    # ...and code / plain text / images / binaries are not.
    assert "text/x-python" not in q
    assert "javascript" not in q
    assert "text/plain" not in q
    assert "image/" not in q
    assert "google-apps.script" not in q
