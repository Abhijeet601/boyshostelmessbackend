import gzip
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .. import repository
from ..db import BACKUP_DIR, CORE_TABLES, backup_database, get_db, enrollment_db
from ..registration import check_identifiers
from ..schemas import Announcement, HostelRequest, SessionRequest, StudentCreateRequest, UserCreateRequest
from ..security import hash_password, require_auth, require_roles
from ..services import authorize_student, local_now, notify, require_hostel, scope, sessions_for, settings, validate_settings
from ..services import attendance_percentages, validate_session_windows

router = APIRouter(tags=["management"])
USER_FIELDS = "id,login_id,role,name,email,mobile,photo_url,hostel_scope,student_id,active,scanner_enabled,created_at"


def staff(user):
    require_roles(user, {"admin", "super-admin"})


def executive(user):
    require_roles(user, {"super-admin"})


@router.get("/public/settings")
def public_settings():
    data = settings()
    return {key: data[key] for key in ("app_name", "institution_name", "logo_url", "timezone", "academic_session", "contact_email", "contact_phone")}


@router.get("/settings")
def get_settings(user=Depends(require_auth)):
    return settings()


@router.put("/settings")
def save_settings(payload: dict = Body(...), user=Depends(require_auth)):
    executive(user)
    config = validate_settings(payload)
    with enrollment_db() as db:
        validate_session_windows(db, config)
        db.execute("INSERT INTO app_settings(id,value_json) VALUES (1,?) ON DUPLICATE KEY UPDATE value_json=VALUES(value_json)", (json.dumps(config),))
        for schedule in config["schedules"]:
            db.execute("UPDATE attendance_sessions SET start_time=?,end_time=?,active=?,timezone=? WHERE custom=0 AND session_date>=? AND meal=?", (schedule["start"],schedule["end"],int(schedule["enabled"]),config["timezone"],local_now(config).date(),schedule["meal"]))
        repository.audit(user["id"], "settings_updated", "settings", 1, db=db)
    return config


@router.get("/students")
def students(q: str = "", hostel: str = "", year: str = "", status: str = "", sort: str = "name", page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), user=Depends(require_auth)):
    staff(user)
    condition, args = scope(user)
    where = [condition, "s.deleted_at IS NULL"]
    if q:
        where.append("(s.name LIKE ? OR s.admission_number LIKE ? OR s.registration_number LIKE ?)")
        args += [f"%{q}%"] * 3
    for key, value in (("hostel", hostel), ("academic_year", year)):
        if value:
            where.append(f"s.{key}=?")
            args.append(value)
    if status in ("active", "inactive"):
        where.append("s.active=?")
        args.append(int(status == "active"))
    order = {"name": "s.name", "roll": "s.admission_number", "newest": "s.id DESC"}.get(sort, "s.name")
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) n FROM students s WHERE {' AND '.join(where)}", args).fetchone()["n"]
        rows = db.execute(f"SELECT s.* FROM students s WHERE {' AND '.join(where)} ORDER BY {order},s.id LIMIT ? OFFSET ?", [*args, page_size, (page-1)*page_size]).fetchall()
        rates = attendance_percentages(db, rows)
        rows = [{**row,"attendance_percentage":rates.get(row["id"])} for row in rows]
        years = db.execute(f"SELECT DISTINCT academic_year FROM students s WHERE {condition} AND academic_year IS NOT NULL", scope(user)[1]).fetchall()
    return {"items": rows, "total": total, "page": page, "page_size": page_size, "years": [r["academic_year"] for r in years]}


@router.get("/students/{student_id}")
def student_detail(student_id: int, user=Depends(require_auth)):
    with get_db() as db:
        student = authorize_student(db, user, student_id)
        app = db.execute("SELECT id,reference,reviewed_at FROM registration_applications WHERE student_id=?", (student_id,)).fetchone()
        history = db.execute("SELECT action,details,created_at FROM audit_logs WHERE entity_type='registration' AND entity_id=? ORDER BY id DESC", (str(app["id"]),)).fetchall() if app else []
        return {**student, "application": app, "approval_history": history}


def write_student(payload, user, student_id=None):
    staff(user)
    creating=student_id is None
    values = payload.model_dump(exclude={"login_id", "password"})
    with enrollment_db() as db:
        require_hostel(db, user, payload.hostel)
        linked = db.execute("SELECT id FROM registration_applications WHERE student_id=?", (student_id or 0,)).fetchone()
        check_identifiers(db, payload.admission_number, payload.registration_number, payload.login_id, linked["id"] if linked else 0, student_id or 0)
        if student_id:
            previous = authorize_student(db, user, student_id, lock=True)
            db.execute(f"UPDATE students SET {','.join(f'{k}=?' for k in values)} WHERE id=?", [*values.values(), student_id])
            db.execute("UPDATE users SET name=?,email=?,mobile=?,photo_url=? WHERE student_id=?",(payload.name,payload.email,payload.mobile,payload.photo_url,student_id))
            db.execute("UPDATE students SET approval_status=? WHERE id=?", ("APPROVED" if payload.active else "SUSPENDED", student_id))
            if bool(previous["active"]) != payload.active:
                db.execute("UPDATE users SET active=?,session_version=session_version+1 WHERE student_id=?", (payload.active, student_id))
        else:
            cursor = db.execute(f"INSERT INTO students({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
            student_id = cursor.lastrowid
        if bool(payload.login_id) != bool(payload.password):
            raise HTTPException(422, "Provide both a login ID and an initial password")
        if payload.login_id:
            if db.execute("SELECT id FROM users WHERE student_id=?", (student_id,)).fetchone():
                raise HTTPException(409, "Student already has a login account")
            db.execute("INSERT INTO users(login_id,password_hash,role,name,email,student_id,active) VALUES (?,?,'student',?,?,?,1)", (payload.login_id, hash_password(payload.password), payload.name, payload.email, student_id))
        repository.audit(user["id"], "student_created" if creating else "student_updated", "student", student_id, db=db)
    return {"id": student_id, "status": "saved"}


@router.post("/students")
def create_student(payload: StudentCreateRequest, user=Depends(require_auth)):
    return write_student(payload, user)


@router.put("/students/{student_id}")
def update_student(student_id: int, payload: StudentCreateRequest, user=Depends(require_auth)):
    return write_student(payload, user, student_id)


@router.delete("/students/{student_id}")
def archive_student(student_id: int, user=Depends(require_auth)):
    staff(user)
    with get_db() as db:
        authorize_student(db, user, student_id, lock=True)
        db.execute("UPDATE students SET active=0,qr_enabled=0,deleted_at=UTC_TIMESTAMP() WHERE id=?", (student_id,))
        db.execute("UPDATE users SET active=0,session_version=session_version+1 WHERE student_id=?", (student_id,))
        repository.audit(user["id"], "student_archived", "student", student_id, db=db)
    return {"status": "archived", "message": "Student archived. Attendance history retained."}


@router.get("/hostels")
def hostels(user=Depends(require_auth)):
    with get_db() as db:
        condition = "1=1"
        args = []
        if user["role"] == "admin":
            condition, args = "h.name=?", [user.get("hostel_scope") or ""]
        elif user["role"] == "student":
            condition, args = "h.name=(SELECT hostel FROM students WHERE id=?)", [user["student_id"]]
        rows = db.execute(f"SELECT h.*,(SELECT COUNT(*) FROM students s WHERE s.hostel=h.name AND s.active=1 AND s.deleted_at IS NULL) students,(SELECT COUNT(*) FROM users u WHERE u.hostel_scope=h.name AND u.role='admin' AND u.active=1) admins FROM hostels h WHERE {condition} ORDER BY h.name", args).fetchall()
    return {"items": rows}


@router.post("/hostels")
def add_hostel(payload: HostelRequest, user=Depends(require_auth)):
    executive(user)
    with get_db() as db:
        cursor = db.execute("INSERT INTO hostels(name,contact,description,active) VALUES (?,?,?,?)", (payload.name, payload.contact, payload.description, payload.active))
        repository.audit(user["id"], "hostel_created", "hostel", cursor.lastrowid, db=db)
    return {"id": cursor.lastrowid}


@router.put("/hostels/{hostel_id}")
def edit_hostel(hostel_id: int, payload: HostelRequest, user=Depends(require_auth)):
    executive(user)
    with enrollment_db() as db:
        old = db.execute("SELECT * FROM hostels WHERE id=? FOR UPDATE", (hostel_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Hostel not found")
        db.execute("UPDATE hostels SET name=?,contact=?,description=?,active=? WHERE id=?", (payload.name, payload.contact, payload.description, payload.active, hostel_id))
        if old["name"] != payload.name:
            db.execute("UPDATE students SET hostel=? WHERE hostel=?", (payload.name, old["name"]))
            db.execute("UPDATE users SET hostel_scope=? WHERE hostel_scope=?", (payload.name, old["name"]))
            db.execute("UPDATE attendance_sessions SET hostel=? WHERE hostel=?", (payload.name, old["name"]))
            db.execute("UPDATE registration_applications SET hostel=? WHERE hostel=?", (payload.name, old["name"]))
        repository.audit(user["id"], "hostel_updated", "hostel", hostel_id, db=db)
    return {"status": "saved"}


@router.get("/admins")
def admins(q: str = "", user=Depends(require_auth)):
    executive(user)
    with get_db() as db:
        rows = db.execute(f"SELECT {USER_FIELDS} FROM users WHERE role IN ('admin','super-admin') AND (name LIKE ? OR login_id LIKE ?) ORDER BY name", (f"%{q}%", f"%{q}%")).fetchall()
    return {"items": rows}


def write_user(payload, user, user_id=None):
    executive(user)
    with enrollment_db() as db:
        linked = db.execute("SELECT id FROM registration_applications WHERE user_id=?", (user_id or 0,)).fetchone()
        check_identifiers(db, login=payload.login_id, user_id=user_id or 0, exclude_application=linked["id"] if linked else 0)
        if payload.role == "admin":
            if not payload.hostel_scope:
                raise HTTPException(422, "Assign a hostel to the administrator")
            require_hostel(db, user, payload.hostel_scope)
        if payload.role == "student":
            authorize_student(db, user, payload.student_id)
        if user_id == user["id"] and (not payload.active or payload.role != "super-admin"):
            raise HTTPException(422, "You cannot remove your own Super Admin access")
        values = payload.model_dump(exclude={"password"})
        if payload.password:
            values["password_hash"] = hash_password(payload.password)
        if user_id:
            if not db.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user_id,)).fetchone():
                raise HTTPException(404, "Account not found")
            db.execute(f"UPDATE users SET {','.join(f'{k}=?' for k in values)},session_version=session_version+1 WHERE id=?", [*values.values(), user_id])
        else:
            if not payload.password:
                raise HTTPException(422, "An initial password is required")
            user_id = db.execute(f"INSERT INTO users({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())).lastrowid
        repository.audit(user["id"], "account_saved", "user", user_id, {"role": payload.role}, db=db)
    return {"id": user_id, "status": "saved"}


@router.post("/admins")
@router.post("/admin/users")
def add_admin(payload: UserCreateRequest, user=Depends(require_auth)):
    return write_user(payload, user)


@router.put("/admins/{user_id}")
def edit_admin(user_id: int, payload: UserCreateRequest, user=Depends(require_auth)):
    return write_user(payload, user, user_id)


@router.get("/sessions")
def sessions(day: date | None = None, user=Depends(require_auth)):
    staff(user)
    with get_db() as db:
        rows = sessions_for(db, day or local_now().date())
    if user["role"] == "admin":
        rows = [s for s in rows if s["hostel"] in ("*", user.get("hostel_scope"))]
    return {"items": rows}


@router.put("/sessions")
def save_session(payload: SessionRequest, user=Depends(require_auth)):
    executive(user)
    if payload.start_time == payload.end_time:
        raise HTTPException(422, "Start and end times must differ")
    with enrollment_db() as db:
        if payload.hostel != "*":
            require_hostel(db, user, payload.hostel)
        validate_session_windows(db, settings(db), payload)
        db.execute("INSERT INTO attendance_sessions(meal,session_date,start_time,end_time,hostel,active,timezone,custom) VALUES (?,?,?,?,?,?,?,1) ON DUPLICATE KEY UPDATE start_time=VALUES(start_time),end_time=VALUES(end_time),active=VALUES(active),timezone=VALUES(timezone),custom=1", (payload.meal, payload.session_date, payload.start_time, payload.end_time, payload.hostel, payload.active, settings(db)["timezone"]))
        repository.audit(user["id"], "session_updated", "session", str(payload.session_date), payload.model_dump(mode="json"), db=db)
    return {"status": "saved"}


@router.get("/scanners")
def scanners(user=Depends(require_auth)):
    executive(user)
    with get_db() as db:
        rows = db.execute("SELECT u.id,u.name,u.hostel_scope,u.scanner_enabled,d.mode,d.last_seen,CASE WHEN d.last_seen>UTC_TIMESTAMP()-INTERVAL 60 SECOND THEN 1 ELSE 0 END online FROM users u LEFT JOIN scanner_devices d ON d.user_id=u.id WHERE u.role IN ('admin','super-admin') AND u.active=1").fetchall()
    return {"items": rows}


@router.get("/qr-security")
def qr_security(user=Depends(require_auth)):
    staff(user)
    condition,args=scope(user)
    with get_db() as db:
        active=db.execute(f"SELECT COUNT(*) n FROM qr_tokens q JOIN students s ON s.id=q.student_id WHERE {condition} AND s.active=1 AND s.qr_enabled=1 AND s.deleted_at IS NULL AND q.used_at IS NULL AND q.expires_at>UTC_TIMESTAMP()",args).fetchone()["n"]
        event_condition,event_args=("1=1",[]) if user["role"]=="super-admin" else ("e.user_id=?",[user["id"]])
        events=db.execute(f"SELECT e.id,e.outcome,e.created_at,u.name operator FROM scan_events e JOIN users u ON u.id=e.user_id WHERE {event_condition} ORDER BY e.id DESC LIMIT 50",event_args).fetchall()
        totals=db.execute(f"SELECT e.outcome,COUNT(*) n FROM scan_events e WHERE {event_condition} GROUP BY e.outcome",event_args).fetchall()
    outcomes={r["outcome"]:r["n"] for r in totals}
    return {"active_tokens":active,"successful":outcomes.get("SUCCESS",0),"rejected":sum(v for k,v in outcomes.items() if k!="SUCCESS"),"outcomes":outcomes,"items":events}


@router.get("/notifications")
def notifications(user=Depends(require_auth)):
    hostel = user.get("hostel_scope")
    with get_db() as db:
        if user["role"] == "student":
            student = authorize_student(db, user, user["student_id"])
            hostel = student["hostel"]
        rows = db.execute("SELECT n.*,CASE WHEN r.user_id IS NULL THEN 1 ELSE 0 END unread FROM notifications n LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=? WHERE (n.user_id=? OR n.user_id IS NULL) AND (n.hostel IS NULL OR n.hostel=?) ORDER BY n.created_at DESC LIMIT 300", (user["id"], user["id"], hostel)).fetchall()
    return {"items": rows, "unread_count": sum(r["unread"] for r in rows)}


@router.post("/notifications/read")
def read_notifications(payload: dict = Body(...), user=Depends(require_auth)):
    allowed = notifications(user)["items"]
    ids = [n["id"] for n in allowed if payload.get("all") or n["id"] == payload.get("id")]
    with get_db() as db:
        for item in ids:
            if payload.get("unread"):
                db.execute("DELETE FROM notification_reads WHERE notification_id=? AND user_id=?", (item, user["id"]))
            else:
                db.execute("INSERT IGNORE INTO notification_reads(notification_id,user_id) VALUES (?,?)", (item, user["id"]))
    return {"status": "saved"}


@router.post("/notifications")
def announce(payload: Announcement, user=Depends(require_auth)):
    staff(user)
    with get_db() as db:
        notify(db, None, payload.title, payload.body, "announcement", user.get("hostel_scope") if user["role"] == "admin" else None)
        repository.audit(user["id"], "announcement_created", "notification", "broadcast", db=db)
    return {"status": "published"}


@router.get("/audit-logs")
def audit_logs(q: str = "", actor: int | None = None, page: int = Query(1, ge=1), user=Depends(require_auth)):
    executive(user)
    where, args = "(a.action LIKE ? OR u.name LIKE ?) AND a.entity_type IN ('student','user','hostel','session','settings','meal_attendance','profile_change_request','meal_menu','notification','backup','scanner','report','registration')", [f"%{q}%", f"%{q}%"]
    if actor:
        where += " AND a.actor_user_id=?"
        args.append(actor)
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) n FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_user_id WHERE {where}", args).fetchone()["n"]
        rows = db.execute(f"SELECT a.*,u.name actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_user_id WHERE {where} ORDER BY a.id DESC LIMIT 30 OFFSET ?", [*args, (page-1)*30]).fetchall()
    return {"items": rows, "total": total}


@router.get("/backups")
def backups(user=Depends(require_auth)):
    executive(user)
    return {"items": [{"name": f.name, "size": f.stat().st_size} for f in sorted(BACKUP_DIR.glob("*.json.gz"), reverse=True)]}


@router.post("/backups")
def create_backup(user=Depends(require_auth)):
    executive(user)
    name = backup_database()
    repository.audit(user["id"], "backup_created", "backup", name)
    return {"name": name}


def backup_path(name):
    path = (BACKUP_DIR / name).resolve()
    if path.parent != BACKUP_DIR.resolve() or not path.is_file() or not name.endswith(".json.gz"):
        raise HTTPException(404, "Backup not found")
    return path


@router.get("/backups/{name}")
def download_backup(name: str, user=Depends(require_auth)):
    executive(user)
    return FileResponse(backup_path(name), filename=name, media_type="application/gzip")


@router.post("/backups/{name}/restore")
def restore_backup(name: str, user=Depends(require_auth)):
    executive(user)
    with gzip.open(backup_path(name), "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("format") != "smartmess-v2":
        raise HTTPException(422, "Unsupported backup format")
    backup_database("before-restore", all_tables=True)
    inserted = 0
    with get_db() as db:
        # Merge missing rows only. Existing values and history are never overwritten.
        for table in CORE_TABLES:
            columns = {r["Field"] for r in db.execute(f"SHOW COLUMNS FROM `{table}`").fetchall()}
            for row in data["tables"].get(table, []):
                values = {k: v for k, v in row.items() if k in columns}
                if values:
                    inserted += db.execute(f"INSERT IGNORE INTO `{table}`({','.join(f'`{k}`' for k in values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())).rowcount
        repository.audit(user["id"], "backup_merged", "backup", name, {"inserted": inserted}, db=db)
    return {"inserted": inserted, "status": "Missing rows restored; existing records retained."}
