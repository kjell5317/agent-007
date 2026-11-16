from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.web import ResearchResult, task_urls  # noqa: E402
from app.agent.tools.schemas import new_input_tools  # noqa: E402
from app.services.handoff import run  # noqa: E402
from app.services.handoff.eligible import handoff_kinds, valid_handoff  # noqa: E402
from app.services.handoff.text import append_section, sources_markdown  # noqa: E402


def _raw(source="gmail", sender="Anna <anna@example.com>", content="", urls=()):
    return SimpleNamespace(
        source=source,
        source_metadata={"from": sender, "urls": list(urls)},
        content=content,
    )


def test_email_reply_is_offered_only_for_gmail_from_a_person():
    assert "email_reply" in handoff_kinds(_raw())
    assert "email_reply" not in handoff_kinds(_raw(sender="GitHub <notifications@github.com>"))
    assert "email_reply" not in handoff_kinds(_raw(sender="no-reply@bank.example"))
    assert "email_reply" not in handoff_kinds(_raw(source="slack"))
    assert handoff_kinds(_raw(source="manual")) == ["web_research", "own_data_lookup", "code_change"]
    assert handoff_kinds(_raw(source="kotx")) == []


def test_valid_handoff_drops_what_the_input_or_label_cannot_take():
    raw = _raw(source="slack")
    assert valid_handoff({"kind": "email_reply", "brief": "Reply"}, raw, label=None, link=None) is None
    assert valid_handoff({"kind": "web_research", "brief": "  "}, raw, label=None, link=None) is None
    assert valid_handoff("web_research", raw, label=None, link=None) is None
    code = {"kind": "code_change", "brief": "Fix the export"}
    assert valid_handoff(code, raw, label="Uni", link=None) is None
    assert valid_handoff(code, raw, label="CSEE", link="https://github.com/a/b/issues/1") is None
    assert valid_handoff(code, raw, label="CSEE", link=None) == code


def test_valid_handoff_keeps_only_a_url_from_the_input():
    raw = _raw(content="Register at https://example.org/workshop by Friday.")
    choice = {"kind": "web_research", "brief": "Find the deadline", "url": "https://example.org/workshop"}
    assert valid_handoff(choice, raw, label=None, link=None)["url"] == "https://example.org/workshop"
    invented = {**choice, "url": "https://evil.example/page"}
    assert "url" not in valid_handoff(invented, raw, label=None, link=None)


def test_task_urls_skips_footers_redirects_and_own_data_links():
    raw = _raw(
        content=(
            "See [the page](https://www.cit.tum.de/electives/](https://www.cit.tum.de/electives/) "
            "and https://app.notion.com/p/Tech-123 or https://c.gle/AAu"
        ),
        urls=[
            "https://www.paypal.com/de/smarthelp/home?v=1",
            "https://example.org/unsubscribe?id=1",
            "https://github.com/a/b/pull/3",
            "https://luma.com/w6hsffwm?a=1&amp;b=2",
            "https://www.linkedin.com/feed/update/1",
        ],
    )
    assert task_urls(raw) == [
        "https://www.cit.tum.de/electives/",
        "https://luma.com/w6hsffwm?a=1&b=2",
    ]


def test_handoff_field_rides_only_on_create_task():
    tools = new_input_tools({}, ["own_data_lookup", "code_change"])
    create = next(t for t in tools if t["name"] == "create_task")
    field = create["parameters"]["properties"]["handoff"]
    assert field["properties"]["kind"]["enum"] == ["own_data_lookup", "code_change"]
    assert "url" not in field["properties"]
    assert "handoff" not in create["parameters"]["required"]
    assert all("handoff" not in t["parameters"]["properties"] for t in tools if t["name"] != "create_task")
    plain = next(t for t in new_input_tools({}) if t["name"] == "create_task")
    assert "handoff" not in plain["parameters"]["properties"]


def test_append_section_keeps_the_existing_description():
    assert append_section(None, "Research", "- Fact") == "**Research**\n\n- Fact"
    assert append_section("Bring ID.\n", "Agent notes", "Done") == "Bring ID.\n\n**Agent notes**\n\nDone"
    assert sources_markdown([("Page", "https://a.example")]) == "Sources: [Page](https://a.example)"


class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,auto", [("web_research", True), ("code_change", False)])
async def test_start_runs_safe_kinds_and_proposes_code_changes(monkeypatch, kind, auto):
    task = SimpleNamespace(id=uuid.uuid4(), title="Fix export")
    created, spawned, proposed = [], [], []

    def fake_create(_session, **fields):
        created.append(fields)
        return SimpleNamespace(id=uuid.uuid4(), brief=fields["brief"])

    async def fake_propose(t, *, brief):
        proposed.append((t, brief))

    monkeypatch.setattr(run.handoffs_store, "active_for_task", lambda *_: None)
    monkeypatch.setattr(run.handoffs_store, "create", fake_create)
    monkeypatch.setattr(run.tasks_store, "get", lambda *_: task)
    monkeypatch.setattr(run, "publish_task", lambda *_: None)
    monkeypatch.setattr(run, "_spawn", spawned.append)
    monkeypatch.setattr(run, "notify_handoff_proposed", fake_propose)

    await run.start(_Session(), task.id, {"kind": kind, "brief": "Do it"})

    assert created[0]["state"] == ("running" if auto else "proposed")
    assert bool(spawned) is auto
    assert proposed == ([] if auto else [(task, "Do it")])


@pytest.mark.asyncio
async def test_web_research_appends_findings_and_moves_only_a_stated_deadline(monkeypatch):
    from app.agent.handoff import research as research_agent
    from app.agent.helpers import web
    from app.services.task import update as update_svc

    due = datetime(2026, 11, 20, 23, 45, tzinfo=timezone.utc)
    new_due = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    task = SimpleNamespace(
        id=uuid.uuid4(), title="Register for the workshop", description="Bring ID.",
        due_date=due, location=None, parent_task_id=None,
    )
    row = SimpleNamespace(url="https://example.org/workshop", brief="Find the deadline")
    updates, notified = [], []

    async def fake_research(_settings, **kwargs):
        assert kwargs["url"] == row.url and kwargs["query"] is None
        return ResearchResult(summary="- Register by Nov 18, 12:00", sources=[("Linked page", row.url)])

    async def fake_correct(_task, findings):
        return {"due_date": new_due}

    async def fake_update(_session, task_id, fields):
        updates.append(fields)

    async def fake_notify(t):
        notified.append(t)

    monkeypatch.setattr(web, "research", fake_research)
    monkeypatch.setattr(research_agent, "correct_fields", fake_correct)
    monkeypatch.setattr(update_svc, "update_task", fake_update)
    monkeypatch.setattr(run, "notify_research_moved_due", fake_notify)
    monkeypatch.setattr(run, "get_settings", lambda: SimpleNamespace())

    state, result = await run._web_research(_Session(), task, row)

    assert state == "ready"
    assert updates[0]["description"].startswith("Bring ID.\n\n**Research**\n\n- Register by Nov 18")
    assert "[Linked page](https://example.org/workshop)" in updates[0]["description"]
    assert updates[0]["due_date"] == new_due
    assert notified == [task]
    assert result["changed"] == ["description", "due_date"]


@pytest.mark.asyncio
async def test_failed_research_leaves_the_task_untouched(monkeypatch):
    from app.agent.helpers import web

    async def fake_research(_settings, **_kwargs):
        return ResearchResult(error="no grounded search results were returned.")

    monkeypatch.setattr(web, "research", fake_research)
    monkeypatch.setattr(run, "get_settings", lambda: SimpleNamespace())
    task = SimpleNamespace(id=uuid.uuid4(), title="t", description="d", due_date=None, location=None)
    with pytest.raises(RuntimeError, match="no grounded search results"):
        await run._web_research(_Session(), task, SimpleNamespace(url=None, brief="q"))


@pytest.mark.asyncio
async def test_manual_start_refuses_a_draft_without_an_email(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), title="Call Tom", label=None, link=None)
    monkeypatch.setattr(run.handoffs_store, "active_for_task", lambda *_: None)
    monkeypatch.setattr(run.raw_inputs_store, "latest_for_task", lambda *_: _raw(source="manual"))
    with pytest.raises(ValueError, match="Drafts need a task from an email"):
        await run.start_manual(_Session(), task, "email_reply", None)


@pytest.mark.asyncio
async def test_a_task_with_a_handoff_is_never_auto_split(monkeypatch):
    from app.services.task import split

    task = SimpleNamespace(id=uuid.uuid4(), title="Write and send the report", estimation=240,
                           parent_task_id=None, is_container=False)

    async def unexpected_split(*_args, **_kwargs):
        raise AssertionError("must not split")

    monkeypatch.setattr(split.handoffs_store, "latest_for_task", lambda *_: SimpleNamespace(kind="web_research"))
    monkeypatch.setattr(split, "split_task", unexpected_split)
    assert await split.maybe_split_task(None, task) == []

