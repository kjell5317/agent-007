"""One bounded decision for each proposed note; shared by ingestion and chat."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import get_settings
from app.db.clients import notes as notes_store
from app.services.input.embedding import embed


_DECISION_TOOL = {
    "name": "moderate_note",
    "description": "Decide whether a proposed durable fact should be saved.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["skip", "merge", "create", "review"]},
            "target_id": {"type": "string", "description": "Existing note id for merge."},
            "content": {"type": "string", "description": "Standalone English fact for create or complete merged fact for merge."},
        },
        "required": ["action"],
    },
}


@dataclass(frozen=True)
class NoteDecision:
    action: str
    content: str
    note_id: uuid.UUID | None


async def moderate_note(
    session,
    content: str,
    *,
    source_raw_input_id: uuid.UUID | None = None,
    source_context: str | None = None,
) -> NoteDecision:
    content = content.strip()
    if not content:
        return NoteDecision("skip", "", None)
    exact = notes_store.find_exact(session, content)
    if exact is not None:
        notes_store.add_source(session, exact.id, source_raw_input_id)
        return NoteDecision("skip", exact.content, exact.id)

    vector = await embed(content)
    candidates = (
        notes_store.search_similar(
            session, embedding=vector, query=content, k=4, min_similarity=0.55
        ) if vector is not None else []
    )
    candidate_ids = {str(candidate.id) for candidate in candidates}
    candidate_lines = "\n".join(
        f"- id={candidate.id}: {candidate.content[:500]}" for candidate in candidates
    ) or "(none)"
    instruction = (
        "Moderate one proposed long-term memory fact. Keep only durable, "
        "standalone English facts anchored to a named subject. Event dates, "
        "venues, agendas, task instructions, and generic website descriptions "
        "belong in events or tasks; skip them. Skip facts already covered by a "
        "candidate. Merge only compatible facts about the same subject, keeping "
        "both complete facts in one standalone note. If candidates contradict "
        "the proposal, choose review. Create only a useful distinct fact. "
        "Use only the provided candidate ids."
    )
    message = (
        f"Proposed note: {content[:1000]}\n"
        f"Source context: {(source_context or '')[:600]}\n"
        f"Existing candidates:\n{candidate_lines}"
    )
    try:
        from app.agent.helpers.llm import chat, user_message

        response = await chat(
            [user_message(message)], get_settings(), system_prompt=instruction,
            tools=[_DECISION_TOOL], force_tool="moderate_note", name="note-moderation",
        )
        decision = next(call.input for call in response.tool_calls if call.name == "moderate_note")
    except Exception:
        decision = {"action": "review", "content": content}

    action = str(decision.get("action") or "review")
    rewritten = str(decision.get("content") or content).strip()
    if action == "skip":
        target = str(decision.get("target_id") or "")
        target_id = uuid.UUID(target) if target in candidate_ids else None
        if target_id is not None:
            notes_store.add_source(session, target_id, source_raw_input_id)
        return NoteDecision("skip", content, target_id)
    if action == "merge" and str(decision.get("target_id") or "") in candidate_ids and rewritten:
        target_id = uuid.UUID(str(decision["target_id"]))
        if rewritten != content:
            notes_store.update(
                session, target_id, content=rewritten, embedding=await embed(rewritten)
            )
        notes_store.add_source(session, target_id, source_raw_input_id)
        return NoteDecision("merge", rewritten, target_id)
    row = notes_store.create(
        session,
        content=rewritten if action == "create" else content,
        source_raw_input_id=source_raw_input_id,
        embedding=vector if rewritten == content else await embed(rewritten),
    )
    if action != "create":
        notes_store.mark_review(session, row.id)
    return NoteDecision("create" if action == "create" else "review", row.content, row.id)
