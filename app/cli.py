import argparse
import getpass

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import CreditAccount, User, UserRole
from app.security import hash_password


def create_admin(username: str, email: str | None) -> None:
    password = getpass.getpass("Admin password: ")
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 characters")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.username == username.lower())):
            raise SystemExit("Username already exists")
        user = User(
            username=username.lower(),
            email=email.lower() if email else None,
            nickname="青葵管理员",
            password_hash=hash_password(password),
            role=UserRole.admin,
        )
        db.add(user)
        db.flush()
        db.add(CreditAccount(user_id=user.id, balance=0))
        db.commit()
    print("Admin created")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("create-admin")
    command.add_argument("--username", required=True)
    command.add_argument("--email")
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.username, args.email)


if __name__ == "__main__":
    main()
