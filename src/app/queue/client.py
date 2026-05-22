"""Background queue client.

Uses RQ (Redis-backed) so the API can return quickly while the agent runs
out-of-band. For a v1 prototype, callers can fall back to
`fastapi.BackgroundTasks` and skip the worker process — keep the interface
narrow enough that the swap is mechanical.
"""

from functools import lru_cache
import uuid

# TODO: import redis + rq lazily so the app can boot without Redis during early dev
# from redis import Redis
# from rq import Queue


@lru_cache
def get_queue():
    # TODO: build Redis connection from settings.redis_url and return rq.Queue("default")
    raise NotImplementedError


def enqueue_process_raw_input(raw_input_id: uuid.UUID) -> str:
    """Schedule agent processing for one raw input. Returns the job id."""
    # TODO: q = get_queue(); job = q.enqueue("app.queue.jobs.run_agent", str(raw_input_id))
    # TODO: return job.id
    raise NotImplementedError
