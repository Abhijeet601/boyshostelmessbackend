import argparse

from .db import get_db, init_db
from .security import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a real backend login ID.")
    parser.add_argument("--login-id", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", choices=["super-admin", "admin", "student"], required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", default="")
    parser.add_argument("--student-id", type=int, default=None)
    args = parser.parse_args()

    init_db()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO users (login_id, password_hash, role, name, email, student_id, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON DUPLICATE KEY UPDATE
              password_hash = VALUES(password_hash),
              role = VALUES(role),
              name = VALUES(name),
              email = VALUES(email),
              student_id = VALUES(student_id),
              active = 1
            """,
            (args.login_id, hash_password(args.password), args.role, args.name, args.email, args.student_id),
        )
    print(f"Ready: {args.role} login ID {args.login_id}")


if __name__ == "__main__":
    main()
