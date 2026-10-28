import gzip
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

from .config import BASE_DIR, MYSQL_DATABASE, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

BACKUP_DIR = Path(BASE_DIR) / "backups"
CORE_TABLES = ["hostels", "students", "users", "revoked_sessions", "app_settings", "attendance_sessions", "qr_tokens", "meal_attendance", "profile_change_requests", "meal_menus", "audit_logs", "notifications", "notification_reads", "scanner_devices", "scan_events"]
CORE_TABLES += ["registration_applications", "registration_photos"]


def row_to_dict(row):
    return row


def _connect(database=None):
    return pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
                           database=database or MYSQL_DATABASE, charset="utf8mb4", cursorclass=DictCursor,
                           autocommit=False, init_command="SET time_zone = '+00:00'")


def _mysql_sql(sql):
    return re.sub(r"\?", "%s", sql.replace("INSERT OR IGNORE", "INSERT IGNORE").replace("%", "%%"))


class MySQLDatabase:
    def __init__(self):
        self.conn = _connect()

    def execute(self, sql, params=None):
        cursor = self.conn.cursor()
        cursor.execute(_mysql_sql(sql), params or ())
        return cursor

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


@contextmanager
def get_db():
    db = MySQLDatabase()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def backup_database(label="manual", all_tables=False):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json.gz"
    with get_db() as db:
        # InnoDB consistent snapshot, including historical tables for migration backups.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        tables = [list(row.values())[0] for row in db.execute("SHOW TABLES").fetchall()]
        data = {table: db.execute(f"SELECT * FROM `{table}`").fetchall() for table in tables if all_tables or table in CORE_TABLES}
    with gzip.open(BACKUP_DIR / name, "wt", encoding="utf-8") as handle:
        json.dump({"format": "smartmess-v2", "created_at": datetime.now(timezone.utc).isoformat(), "tables": data}, handle, default=str)
    return name


@contextmanager
def enrollment_db():
    # This connection lock spans commit and all public/staff identifier writers.
    with get_db() as db:
        lock_name = "enrollment:" + MYSQL_DATABASE[:50]
        acquired = db.execute("SELECT GET_LOCK(?,10) acquired", (lock_name,)).fetchone()["acquired"]
        if acquired != 1:
            from fastapi import HTTPException
            raise HTTPException(503, "Registration is busy. Please retry.")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("SELECT RELEASE_LOCK(?)", (lock_name,))


def _split_sql(script):
    return [part.strip() for part in script.split(";") if part.strip()]


def init_db():
    # The configured database already exists in deployments. Creation is safe and idempotent.
    server = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD, charset="utf8mb4")
    try:
        with server.cursor() as cursor:
            if not re.fullmatch(r"[A-Za-z0-9_]+", MYSQL_DATABASE):
                raise ValueError("Invalid database name")
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        server.commit()
    finally:
        server.close()
    with get_db() as db:
        tables = {list(row.values())[0] for row in db.execute("SHOW TABLES").fetchall()}
    if "students" in tables and "app_settings" not in tables:
        backup_database("before-smartmess", all_tables=True)
    if "students" in tables and "registration_applications" not in tables:
        backup_database("before-registration", all_tables=True)
    schema = "\n".join((Path(BASE_DIR) / name).read_text(encoding="utf-8-sig") for name in ("mysql_schema.sql", "smartmess_schema.sql", "registration_schema.sql"))
    schema = "\n".join(line for line in schema.splitlines() if not line.lstrip().startswith("--"))
    with get_db() as db:
        for statement in _split_sql(schema):
            db.execute(statement)
        additions = {
            "students": {"department": "VARCHAR(120) NULL", "semester": "VARCHAR(40) NULL", "qr_enabled": "TINYINT NOT NULL DEFAULT 1", "deleted_at": "DATETIME NULL", "approval_status": "VARCHAR(20) NOT NULL DEFAULT 'APPROVED'", "academic_session": "VARCHAR(80) NULL"},
            "users": {"mobile": "VARCHAR(30) NULL", "photo_url": "TEXT NULL", "session_version": "INT NOT NULL DEFAULT 0", "scanner_enabled": "TINYINT NOT NULL DEFAULT 1"},
            "meal_attendance": {"session_id": "BIGINT UNSIGNED NULL", "hostel_at_scan": "VARCHAR(120) NULL"},
            "profile_change_requests": {"original_json": "TEXT NULL"},
            "attendance_sessions": {"custom": "TINYINT NOT NULL DEFAULT 0"},
        }
        for table, fields in additions.items():
            columns = {r["COLUMN_NAME"] for r in db.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", (MYSQL_DATABASE, table)).fetchall()}
            for name, declaration in fields.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {declaration}")
        indexes = {r["INDEX_NAME"] for r in db.execute("SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=? AND TABLE_NAME='meal_attendance'", (MYSQL_DATABASE,)).fetchall()}
        if "uq_attendance_session" not in indexes:
            db.execute("CREATE UNIQUE INDEX uq_attendance_session ON meal_attendance(student_id,session_id)")
        db.execute("INSERT IGNORE INTO hostels(name) SELECT DISTINCT hostel FROM students WHERE hostel<>''")
        db.execute("INSERT IGNORE INTO hostels(name) SELECT DISTINCT hostel_scope FROM users WHERE hostel_scope IS NOT NULL AND hostel_scope<>''")
