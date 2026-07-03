# Staged hybrid search — implementation plan

Search across everything the agent knows (tasks, raw inputs, notes,
calendar, kotx briefs) plus federated live search into Drive, Notion and GitHub.
Two latency tiers: instant suggestions while typing, hybrid semantic +
keyword results on Enter, and later a chat/"ask" mode on the same
retrieval layer.

Design rule carried over from the agent: local corpus in Postgres
(pgvector + FTS) for anything we own or can cheaply mirror; federated
live calls for corpora too big to mirror. External reference content is
**not** ingested as `raw_inputs` — it is reference corpus, not input for
the task agent to decide on.

## Foundation: the `documents` table

General external-reference index, not calendar-only.

| Column | Notes |
|---|---|
| `id` | uuid pk |
| `provider` | `calendar` \| `notion` \| `drive` \| `kotx` \| … |
| `external_id` | unique with `provider` (upsert key) |
| `title`, `snippet` | display fields |
| `content` | indexed text (may be empty for metadata-only providers) |
| `url` | deep link into the provider |
| `metadata` | jsonb, per-provider fields |
| `starts_at`, `ends_at` | nullable real columns — calendar interval queries |
| `updated_at` | provider-side modification time |
| `embedding` | `vector(1536)`, Gemini, same space as inputs/notes |
| `tsv` | generated tsvector column, GIN index |

Also `pg_trgm` GIN indexes on titles (here and on `tasks.title`,
`notes.content`) for prefix/typo matching.

New `app/services/search/` service + thin `app/api/search.py` router.
Sync jobs live with the provider services, never in the agent layer.

Planned occupants:
- **calendar** — events (see cache section below).
- **kotx** — `TASK.md`, `REVIEW.md`, and proposed PR title/body per kotx
  task, refreshed on the state webhook; readable after terminal states
  (guaranteed by the kotx API changes, see
  `docs/kotx-consolidation-prompt.md`).

## Stage 1 — suggest-as-you-type

Target <50 ms, zero external API calls, zero embeddings (embedding 1–2
typed words is slow, costs per keystroke, and ranks poorly).

- One UNION SQL query across `tasks`, `notes`, `raw_inputs`, `documents`,
  each row tagged with its type; limit ~8.
- Matching: `websearch_to_tsquery` with a `:*` prefix on the last token,
  plus trigram match on titles for typos.
- Ranking: `ts_rank` × the same exponential recency decay used elsewhere
  (`exp(-age/half_life)`).
- Filter tokens parsed out of the query before matching: `source:gmail`,
  `label:uni`, `is:open`, `before:2026-06`, `provider:calendar`.
- Zero-query state: most recent items.
- Client: `GET /search/suggest?q=…`, debounce ~150 ms.

## Stage 2 — hybrid results on Enter

Budget ~1–2 s. Two paths run in parallel.

**Local hybrid (one SQL statement):**
- One Gemini embedding call for the query (`SEMANTIC_SIMILARITY`, same
  task type as stored vectors so they're comparable).
- Two CTEs — pgvector cosine top-40 and FTS top-40 — fused with
  Reciprocal Rank Fusion: `score = Σ 1/(60 + rank)`. RRF is rank-based,
  so cosine scores and `ts_rank` never need to be made comparable.

**Federated fan-out (`asyncio.gather`, ~2 s per-provider timeout):**
- **Drive**: `files.list` with `q="fullText contains '<q>'"`,
  `fields=id,name,mimeType,modifiedTime,webViewLink`. Needs
  `drive.readonly` added to `DEFAULT_SCOPES` in `app/auth/google.py`
  (one re-consent).
- **GitHub**: `/search/issues` + `/search/code` via a new
  `@register_provider("github")` OAuth provider. Separately, GitHub also
  becomes an ingestion source (notifications → raw_inputs) — independent
  work item.
- **Notion**

**Merge/UI:** local RRF list first, federated results appended in
provider-labeled groups — cross-system scores aren't comparable and
grouped sections degrade gracefully on provider timeout. Frontend fires
the local and per-provider requests in parallel and fills sections as
they land (no SSE needed).

### Calendar cache (also serves rescheduling)

- Sync all events from `google_calendar_id` + `google_busy_calendar_ids`
  into `documents` (provider=`calendar`), embedding
  `summary + location + description`; `starts_at`/`ends_at` set.
- Incremental via the Calendar API `syncToken`; per-calendar sync-state
  row (`calendar_id`, `sync_token`, `last_synced_at`) — same pattern as
  the route cache.
- **Write-through is mandatory**: every `create_event` / `patch_event` /
  `delete_event` in `app/services/calendar/events.py` upserts/deletes the
  cache row synchronously. Otherwise the next schedule pass double-books
  against the app's own just-written events.
- **Two freshness tiers:**
  - Search/suggest reads: accept TTL staleness (10–30 min).
  - Scheduling reads (inside the `schedule_task` lock): always do an
    incremental `syncToken` refresh first — the delta call is near-free
- Bonus: `find_calendar_events` / event-dedup in the agent can query the
  cache semantically ("Team offsite" matches "Offsite planning").
- be careful to don't create too many embeddings calls.

## Stage 3 — chat / "ask" mode

Separate affordance from the search box. An agent loop (same runner
pattern as the input agents) with the stage-1/2 retrieval functions
exposed as tools (`search_local`, `search_drive`, `search_github`,
`search_notes`), streaming an answer with links to hits.

**Notion — MCP vs REST decision:** Notion's hosted MCP search is backed
by their internal semantic search and beats the public REST
`/v1/search` (title-biased) in quality, but it is agent-facing: no
stable ranking scores to fuse into RRF, extra latency, unusable for
typeahead. Decision: REST API for the sync-and-index path (stages 1–2);
Notion MCP as a chat-mode tool (stage 3). If Notion only ever matters in
chat mode, MCP alone suffices and the sync job can be skipped.

## Build order

1. `documents` table + tsvector/trgm migrations + `GET /search/suggest`
   over existing data (tasks/inputs/notes) — no new integrations.
2. Hybrid RRF `GET /search` endpoint.
3. Calendar cache sync (reuses existing OAuth + client) + scheduler
   read-through + write-through.
4. Drive federated (scope add + one endpoint).
5. GitHub OAuth provider + federated; GitHub/kotx ingestion per the
   consolidation plan.
6. Notion sync (or MCP-only if chat-mode-only).
7. Chat mode.

Each step ships something usable on its own.

## Related groundwork already shipped (2026-07-03)

- Notes are saved from **all** terminal agent decisions (`create_task`,
  `update_task`, `no_change`, `mark_not_task`) across the new-input,
  thread-followup, and extraction flows — not only from rejections.
- `search_notes` results carry provenance: date, from, subject, sim.
- Precedent search and the auto-decide threshold use recency-decayed
  similarity (`INPUT_SIMILARITY_HALF_LIFE_DAYS`; notes use
  `NOTES_SIMILARITY_HALF_LIFE_DAYS`). Note the formula is e-folding
  (`exp(-age/days)`), not a true half-life — tune accordingly.

## Open follow-ups (search-adjacent, not scheduled)

- Notes API/UI so long-term memory is auditable and correctable
  (`notes.list_recent` exists but is unused); note dedup/contradiction
  handling.
- Precedent quota: candidates are ranked by decayed similarity; watch
  whether very old but exact matches get crowded out.
