"""Job functions executed by the RQ worker.

Workers import these by string name, so keep signatures stable and
plain-Python (no FastAPI request objects, no async-only code paths).
"""

def run_agent(raw_input_id: str) -> None:
    """Worker entry point: process one RawInput end-to-end."""
    # TODO: open a SessionLocal(), call app.agent.process_raw_input(session, UUID(raw_input_id))
    # TODO: handle retries / dead-letter on agent errors (rq has built-in retry)
    raise NotImplementedError(raw_input_id)


# TODO: periodic poll job per source (scheduled via rq-scheduler or cron)
# TODO: nightly embedding-refresh job if the embedding model changes
