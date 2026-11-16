from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.helpers.web import ResearchResult, task_urls  # noqa: E402
from app.agent.tools.schemas import new_input_tools  # noqa: E402
from app.services.handoff import run  # noqa: E402
from app.services.handoff.eligible import handoff_kinds, valid_answer, valid_handoff  # noqa: E402
from app.services.handoff.text import append_section  # noqa: E402

REPOS = {"Aflatoun": "TUM-Social-AI/aflaconnect"}


def _raw(source="gmail", sender="Anna <anna@example.com>", content="", urls=()):
    return SimpleNamespace(
        source=source,
        source_metadata={"from": sender, "urls": list(urls)},
        content=content,
    )


def _response(*calls):
    return LLMResponse(
        message=LLMMessage(role="assistant", tool_calls=tuple(calls)), tool_calls=tuple(calls),
        text="", stop_reason="tool_use", usage={}, meta={}, provider="test", model="test",
    )


class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_kinds_offered_depend_on_the_source_and_repo_labels():
    assert handoff_kinds(_raw(), REPOS) == ["research", "email_reply", "code_change"]
    assert handoff_kinds(_raw(sender="GitHub <notifications@github.com>"), REPOS) == ["research", "code_change"]
    assert handoff_kinds(_raw(sender="no-reply@bank.example"), {}) == ["research"]
    assert handoff_kinds(_raw(source="slack"), REPOS) == ["research", "code_change"]
    assert handoff_kinds(_raw(source="kotx"), REPOS) == []


def test_a_code_change_needs_a_label_connected_to_a_repo():
    raw = _raw(source="slack")
    code = {"kind": "code_change", "brief": "Fix the export"}
    assert valid_handoff(code, raw, label="CSEE", link=None, repo_labels=REPOS) is None
    assert valid_handoff(
        code, raw, label="Aflatoun", link="https://github.com/a/b/issues/1", repo_labels=REPOS,
    ) is None
    assert valid_handoff(code, raw, label="Aflatoun", link=None, repo_labels=REPOS) == code


def test_valid_handoff_drops_what_the_input_was_not_offered():
    raw = _raw(source="slack")
    assert valid_handoff({"kind": "email_reply", "brief": "Reply"}, raw, label=None, link=None, repo_labels={}) is None
    assert valid_handoff({"kind": "research", "brief": "  "}, raw, label=None, link=None, repo_labels={}) is None
    assert valid_handoff("research", raw, label=None, link=None, repo_labels={}) is None
    assert valid_handoff(
        {"kind": "research", "brief": "Find the deadline", "url": "x"}, raw, label=None, link=None, repo_labels={},
    ) == {"kind": "research", "brief": "Find the deadline"}
    assert valid_answer("  ") is None and valid_answer(" 42 ") == "42"


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


def test_handoff_and_answer_ride_only_on_create_task():
    tools = new_input_tools({}, ["research", "code_change"], ["Aflatoun"])
    create = next(t for t in tools if t["name"] == "create_task")
    props = create["parameters"]["properties"]
    assert props["handoff"]["properties"]["kind"]["enum"] == ["research", "code_change"]
    assert set(props["handoff"]["properties"]) == {"kind", "brief"}
    assert "labels with a repo: Aflatoun" in props["handoff"]["description"]
    assert "answer" in props and "answer" not in create["parameters"]["required"]
    others = [t for t in tools if t["name"] != "create_task"]
    assert all({"handoff", "answer"}.isdisjoint(t["parameters"]["properties"]) for t in others)
    plain = next(t for t in new_input_tools({}) if t["name"] == "create_task")
    assert {"handoff", "answer"}.isdisjoint(plain["parameters"]["properties"])


def test_append_section_keeps_the_existing_description():
    assert append_section(None, "Research", "- Fact") == "**Research**\n\n- Fact"
    assert append_section("Bring ID.\n", "Answer", "Done") == "Bring ID.\n\n**Answer**\n\nDone"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,auto", [("research", True), ("code_change", False)])
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


def _research_task():
    return SimpleNamespace(
        id=uuid.uuid4(), title="Find the landlord's invoice number", description="For the tax form.",
        due_date=datetime(2026, 11, 20, 23, 45, tzinfo=timezone.utc), location=None, parent_task_id=None,
    )


def _patch_research(monkeypatch, report):
    from app.agent.handoff import research as research_agent
    from app.services.task import close as close_svc
    from app.services.task import update as update_svc

    calls = {"update": [], "close": [], "answered": [], "moved": []}

    async def fake_run(_session, _task, brief, links):
        calls["brief"], calls["links"] = brief, links
        return report

    async def fake_update(_session, _task_id, fields):
        calls["update"].append(fields)

    async def fake_close(_session, task_id, **kwargs):
        calls["close"].append((task_id, kwargs))

    async def fake_answered(task, answer):
        calls["answered"].append(answer)

    async def fake_moved(task):
        calls["moved"].append(task)

    monkeypatch.setattr(research_agent, "run_research", fake_run)
    monkeypatch.setattr(update_svc, "update_task", fake_update)
    monkeypatch.setattr(close_svc, "close_task", fake_close)
    monkeypatch.setattr(run, "notify_task_answered", fake_answered)
    monkeypatch.setattr(run, "notify_research_moved_due", fake_moved)
    monkeypatch.setattr(run.raw_inputs_store, "latest_for_task", lambda *_: _raw(
        content="Invoice from https://example.org/invoice",
    ))
    return calls


@pytest.mark.asyncio
async def test_research_that_completes_the_task_closes_it_and_notifies(monkeypatch):
    from app.agent.handoff.research import ResearchReport

    task = _research_task()
    report = ResearchReport(
        findings="- Invoice number: 2026-117 (email from Hausverwaltung, 3 Nov)",
        completes_task=True,
        due_date=datetime(2026, 11, 18, tzinfo=timezone.utc),
        trace={"branch": "handoff_research", "iterations": [], "outcome": "closed"},
    )
    calls = _patch_research(monkeypatch, report)

    state, result, trace = await run._research(_Session(), task, SimpleNamespace(brief="Find it"))

    assert state == "ready" and result["completed"] is True and trace["outcome"] == "closed"
    assert calls["links"] == ["https://example.org/invoice"]
    assert calls["update"] == [{"description": "For the tax form.\n\n**Research**\n\n" + report.findings}]
    assert calls["close"] == [(task.id, {"award_points": False})]
    assert calls["answered"] == [report.findings]
    assert calls["moved"] == []


@pytest.mark.asyncio
async def test_research_that_leaves_work_moves_only_a_stated_deadline(monkeypatch):
    from app.agent.handoff.research import ResearchReport

    task = _research_task()
    new_due = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    report = ResearchReport(findings="- Register by Nov 18, 12:00", completes_task=False, due_date=new_due)
    calls = _patch_research(monkeypatch, report)

    state, result, _trace = await run._research(_Session(), task, SimpleNamespace(brief="Find it"))

    assert result["changed"] == ["description", "due_date"]
    assert calls["update"][0]["due_date"] == new_due
    assert calls["close"] == [] and calls["answered"] == []
    assert calls["moved"] == [task]


@pytest.mark.asyncio
async def test_research_agent_searches_reads_links_and_reports(monkeypatch):
    from app.agent.handoff import research as research_agent

    replies = iter([
        _response(
            ToolCall(id="1", name="messages_search", input={"query": "invoice Hausverwaltung"}),
            ToolCall(id="2", name="read_link", input={"url": "https://example.org/invoice"}),
        ),
        _response(ToolCall(id="3", name="report", input={
            "findings": "- Invoice number: 2026-117", "completes_task": True,
        })),
    ])
    offered, read = [], []

    async def fake_chat(_messages, _settings, **kwargs):
        offered.append([t["name"] for t in kwargs["tools"]])
        assert kwargs["force_tool"] == "any"
        return next(replies)

    async def fake_tool(_session, _cites, call):
        return "Hausverwaltung, 3 Nov: invoice 2026-117"

    async def fake_read(_settings, *, url, context):
        read.append(url)
        return ResearchResult(summary="- Invoice 2026-117", sources=[("Linked page", url)])

    monkeypatch.setattr(research_agent, "chat", fake_chat)
    monkeypatch.setattr(research_agent, "read_only_tools", lambda _s: [{"name": "messages_search"}])
    monkeypatch.setattr(research_agent, "run_read_only_tool", fake_tool)
    monkeypatch.setattr(research_agent, "research", fake_read)
    monkeypatch.setattr(research_agent, "get_settings", lambda: SimpleNamespace(
        user_timezone="UTC", chat_thinking_level="low", search_chat_max_iterations=5,
        llm_target=lambda flow: ("google", "gemini-3.8-flash"),
    ))

    report = await research_agent.run_research(None, _research_task(), "Find it", ["https://example.org/invoice"])

    assert offered[0] == ["messages_search", "read_link", "report"]
    assert read == ["https://example.org/invoice"]
    assert report.completes_task and report.findings == "- Invoice number: 2026-117"
    results = report.trace["iterations"][0]["tool_results"]
    assert [r["name"] for r in results] == ["messages_search", "read_link"]
    assert report.trace["outcome"] == "closed"


@pytest.mark.asyncio
async def test_manual_start_checks_what_the_task_can_take(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), title="Call Tom", label="CSEE", link=None)
    monkeypatch.setattr(run.handoffs_store, "active_for_task", lambda *_: None)
    monkeypatch.setattr(run.raw_inputs_store, "latest_for_task", lambda *_: _raw(source="manual"))
    monkeypatch.setattr(run.labels_store, "repos_by_name", lambda _s: REPOS)
    with pytest.raises(ValueError, match="Drafts need a task from an email"):
        await run.start_manual(_Session(), task, "email_reply", None)
    with pytest.raises(ValueError, match="label connected to a GitHub repo"):
        await run.start_manual(_Session(), task, "code_change", None)


@pytest.mark.asyncio
async def test_a_task_with_a_handoff_is_never_auto_split(monkeypatch):
    from app.services.task import split

    task = SimpleNamespace(id=uuid.uuid4(), title="Write and send the report", estimation=240,
                           parent_task_id=None, is_container=False)

    async def unexpected_split(*_args, **_kwargs):
        raise AssertionError("must not split")

    monkeypatch.setattr(split.handoffs_store, "latest_for_task", lambda *_: SimpleNamespace(kind="research"))
    monkeypatch.setattr(split, "split_task", unexpected_split)
    assert await split.maybe_split_task(None, task) == []


@pytest.mark.asyncio
async def test_an_answer_saves_the_task_as_done_and_notifies(monkeypatch):
    from app.agent.input import runner as input_runner

    task_id = uuid.uuid4()
    created, finalized, answered = [], {}, []

    async def fake_chat(_messages, *_args, **_kwargs):
        return _response(ToolCall(id="c", name="create_task", input={
            "title": "Look up the invoice number", "estimation": 15,
            "due_date": "2026-11-20T10:00:00+00:00", "label": "Privat",
            "answer": "2026-117 (note saved from the Hausverwaltung email)",
            "handoff": {"kind": "research", "brief": "Find the invoice number"},
        }))

    def fake_create(_session, payload):
        created.append(payload)
        return SimpleNamespace(id=task_id, title=payload.title, label=payload.label, link=payload.link)

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("an answered task is neither scheduled nor handed off")

    async def fake_answered(task, answer):
        answered.append(answer)

    monkeypatch.setattr(input_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(input_runner, "chat", fake_chat)
    monkeypatch.setattr(input_runner.tasks, "create", fake_create)
    monkeypatch.setattr(input_runner, "schedule_task", unexpected)
    monkeypatch.setattr(input_runner, "maybe_split_task", unexpected)
    monkeypatch.setattr(input_runner, "start_handoff", unexpected)
    monkeypatch.setattr(input_runner, "notify_task_answered", fake_answered)
    monkeypatch.setattr(input_runner.raw_inputs, "finalize", lambda _s, _id, **kw: finalized.update(kw))

    raw = SimpleNamespace(
        id=uuid.uuid4(), source="manual", source_metadata={}, content="What's the landlord's invoice number?",
    )
    trace = await input_runner.run_new_input_agent(SimpleNamespace(commit=lambda: None), raw, [], None)

    assert created[0].description == "**Answer**\n\n2026-117 (note saved from the Hausverwaltung email)"
    assert finalized["status"] == "closed" and finalized["task_id"] == task_id
    assert trace["answer"].startswith("2026-117") and "handoff" not in trace
    assert answered == ["2026-117 (note saved from the Hausverwaltung email)"]
