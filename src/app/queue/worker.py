"""Standalone RQ worker entry point.

Run with:
    python -m app.queue.worker
"""


def main() -> None:
    # TODO: build Redis connection from settings.redis_url
    # TODO: rq.Worker(["default"], connection=...).work()
    raise NotImplementedError


if __name__ == "__main__":  # pragma: no cover
    main()
