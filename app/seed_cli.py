from app.db import SessionLocal
from app.seed import seed_demo_content


if __name__ == "__main__":
    with SessionLocal() as session:
        seed_demo_content(session)
