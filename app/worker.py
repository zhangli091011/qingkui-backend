from redis import Redis
from rq import Queue, Worker

from app.config import settings


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    queue_names = list(dict.fromkeys((settings.ocr_queue_name, settings.contribution_queue_name)))
    queues = [Queue(name, connection=connection) for name in queue_names]
    Worker(queues, connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
