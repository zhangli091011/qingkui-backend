from redis import Redis
from rq import Queue, Worker

from app.config import settings


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.ocr_queue_name, connection=connection)
    Worker([queue], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
