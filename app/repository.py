import json

from .db import get_db


def audit(actor_user_id, action, entity_type, entity_id, details="", db=None):
    payload = json.dumps(details if isinstance(details, (dict, list)) else {"message": details}, default=str)
    sql = "INSERT INTO audit_logs (actor_user_id,action,entity_type,entity_id,details) VALUES (?,?,?,?,?)"
    args = (actor_user_id, action, entity_type, str(entity_id), payload)
    if db is not None:
        db.execute(sql, args)
    else:
        with get_db() as connection:
            connection.execute(sql, args)


def get_user_by_login_id(login_id):
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE lower(login_id)=lower(?) AND active=1", (login_id,)).fetchone()


def get_student(student_id):
    with get_db() as db:
        return db.execute("SELECT * FROM students WHERE id=? AND active=1 AND approval_status='APPROVED' AND deleted_at IS NULL", (student_id,)).fetchone()
