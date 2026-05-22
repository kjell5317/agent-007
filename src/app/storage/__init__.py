"""Database-facing operations (CRUD + vector search).

Kept separate from API routes so the agent runner can call the same code paths
without going through HTTP.
"""

# TODO: tasks.create / tasks.update / tasks.list / tasks.search_similar
# TODO: raw_inputs.create / raw_inputs.mark_processed
# TODO: feedback.create
# TODO: oauth_tokens.upsert / oauth_tokens.get (with decryption)
