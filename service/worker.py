from __future__ import annotations

import logging
import time

from redis.exceptions import RedisError

from service.database import RunStore
from service.queue import RedisJobQueue
from service.runner import RunService
from service.settings import ServiceSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = ServiceSettings.from_env()
    store = RunStore(settings.database_url)
    store.init_schema()
    queue = RedisJobQueue(settings.redis_url)
    runner = RunService(settings, store, queue)
    logging.info("PatchFlow worker started")
    while True:
        try:
            run_id = queue.dequeue(timeout=5)
        except RedisError as exc:
            logging.warning("Redis queue unavailable, retrying: %s", exc)
            time.sleep(1)
            continue
        if run_id:
            runner.execute(run_id)


if __name__ == "__main__":
    main()
