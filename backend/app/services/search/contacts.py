"""Google People API federated contact search for chat retrieval.

A live `people:searchContacts` call over the user's own contacts, run on demand
like the Drive federation — contacts are reference data (who someone is, their
email/phone), not task input, so they're never mirrored.

Best-effort by contract: no Google connection, an expired grant, a timeout or
any API error returns `[]` so the answer never blocks on contacts.

The People search endpoint reads from a server-side cache that a cold account
may not have populated yet; Google's documented remedy is a "warmup" request
with an empty query. We only pay for it when a live search comes back empty.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy.orm import Session

from app.auth.google_tokens import GoogleTokenError, get_fresh_google_token
from app.db.schemas.search import SearchHit

log = logging.getLogger(__name__)

_SEARCH_URL = "https://people.googleapis.com/v1/people:searchContacts"
_READ_MASK = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,relations"


class ContactsClient:
    def __init__(self, access_token: str, *, timeout: float = 5.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def search(self, query: str, *, limit: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            results = await self._query(client, query, limit)
            if not results:
                # Cold People cache: prime it with an empty-query warmup, then retry.
                await self._query(client, "", 1)
                results = await self._query(client, query, limit)
            return results

    async def _query(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        resp = await client.get(
            _SEARCH_URL,
            params={"query": query, "readMask": _READ_MASK, "pageSize": limit},
        )
        resp.raise_for_status()
        return [r["person"] for r in resp.json().get("results", []) if r.get("person")]


async def search_contacts(
    session: Session, query: str, *, k: int, timeout: float
) -> list[SearchHit]:
    query = (query or "").strip()
    if not query:
        return []
    try:
        token = await get_fresh_google_token(session)
        people = await asyncio.wait_for(
            ContactsClient(token.access_token, timeout=timeout).search(query, limit=k),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, GoogleTokenError, httpx.HTTPError) as exc:
        log.info("contacts search skipped · %s: %s", type(exc).__name__, exc)
        return []
    return [_to_hit(p) for p in people]


def _to_hit(person: dict) -> SearchHit:
    resource = str(person.get("resourceName") or "")  # e.g. "people/c123"
    name = _primary(person.get("names"), "displayName") or "(no name)"
    emails = [e["value"] for e in person.get("emailAddresses", []) if e.get("value")]
    phones = [p["value"] for p in person.get("phoneNumbers", []) if p.get("value")]
    org = _primary(person.get("organizations"), "name")
    addresses = [a["formattedValue"] for a in person.get("addresses", []) if a.get("formattedValue")]
    birthday = _birthday(person.get("birthdays"))
    relations = _relations(person.get("relations"))
    meta = {
        k: v
        for k, v in {
            "emails": emails,
            "phones": phones,
            "addresses": addresses,
            "org": org,
            "birthday": birthday,
            "relations": relations,
        }.items()
        if v
    }
    return SearchHit(
        type="contact",
        id=resource,
        title=name,
        snippet=" · ".join(emails + phones) or None,
        url=_web_url(resource),
        source="contacts",
        status="contact",
        meta=meta or None,
        score=0.0,
    )


def _primary(items: list[dict] | None, field: str) -> str | None:
    """First value for a repeated People field, preferring the primary one."""
    for item in items or []:
        if (item.get("metadata") or {}).get("primary"):
            return item.get(field)
    return (items or [{}])[0].get(field) if items else None


def _birthday(items: list[dict] | None) -> str | None:
    # People returns a google.type.Date whose `year` is often absent, plus an
    # optional free-form `text`. Primary-first so an account birthday wins over
    # a contact-entered one.
    ordered = sorted(items or [], key=lambda i: not (i.get("metadata") or {}).get("primary"))
    for item in ordered:
        date = item.get("date") or {}
        if date.get("month") and date.get("day"):
            year, month, day = date.get("year"), date["month"], date["day"]
            return f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"
        if item.get("text"):
            return item["text"]
    return None


def _relations(items: list[dict] | None) -> list[str]:
    out = []
    for item in items or []:
        person = item.get("person")
        if not person:
            continue
        label = item.get("formattedType") or item.get("type")
        out.append(f"{person} ({label})" if label else person)
    return out


def _web_url(resource: str) -> str | None:
    person_id = resource.split("/", 1)[-1] if "/" in resource else resource
    return f"https://contacts.google.com/person/{person_id}" if person_id else None
