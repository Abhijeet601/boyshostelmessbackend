import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pymysql
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder

from .. import repository
from ..db import get_db
from ..schemas import MealMenuUpsert, ProfileChangeCreate, ProfileChangeDecision, QRConsumeRequest, StudentCreateRequest
from pydantic import ValidationError
from ..security import require_auth, require_roles
from ..services import active_session, authorize_student, local_now, notify, rate_limit, scope, sessions_for, settings, utc_iso

router = APIRouter(tags=["attendance portal"])
PROFILE_FIELDS = {"name", "email", "mobile", "room_number", "course", "academic_year", "photo_url", "department", "semester"}


def failure(code, message, status=400, **extra):
    raise HTTPException(status, jsonable_encoder({"success": False, "code": code, "message": message, **extra}))


def scanner_student(student):
    """Return only the profile fields needed at an authorized scanner."""
    fields = ("id", "name", "admission_number", "registration_number", "photo_url", "hostel", "room_number")
    result = {field: student.get(field) for field in fields}
    return {**result, "rollNumber": result["admission_number"],
            "registrationNumber": result["registration_number"],
            "photoUrl": result["photo_url"], "room": result["room_number"]}


def existing_meal_attendance(db, student_id, session):
    # A current locking read observes a concurrent scanner's committed insert,
    # even if the earlier QR lookup established an older InnoDB snapshot.
    return db.execute(
        "SELECT id,scanned_at,qr_token_id,session_id FROM meal_attendance "
        "WHERE student_id=? AND meal_date=? AND meal_type=? FOR UPDATE",
        (student_id, session["session_date"], session["meal"]),
    ).fetchone()


def duplicate_attendance(student, session, attendance, replay=False):
    marked_at = utc_iso(attendance["scanned_at"])
    failure("ALREADY_MARKED", "Attendance already marked for this meal", 409,
            student=scanner_student(student), session=session, meal=session["meal"],
            attendance_id=attendance["id"], original_time=marked_at,
            alreadyMarkedAt=marked_at, qr_replay=replay)


@router.get("/student/me")
def student_me(user=Depends(require_auth)):
    require_roles(user, {"student"})
    student = repository.get_student(user.get("student_id"))
    if not student:
        raise HTTPException(404, "Student profile is inactive or unlinked")
    return student


@router.post("/student/qr")
def issue_qr(user=Depends(require_auth)):
    require_roles(user, {"student"})
    rate_limit(f"qr:{user['id']}", 20, 60)
    token = secrets.token_urlsafe(32)
    with get_db() as db:
        student = authorize_student(db, user, user.get("student_id"), lock=True)
        if not student["active"] or student["approval_status"] != "APPROVED" or student["deleted_at"] or not student["qr_enabled"]:
            failure("STUDENT_INACTIVE", "Your QR access is disabled", 403)
        config = settings(db)
        session = active_session(db, student["hostel"])
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=config["qr_ttl_seconds"])
        db.execute("UPDATE qr_tokens SET expires_at=UTC_TIMESTAMP() WHERE student_id=? AND used_at IS NULL AND expires_at>UTC_TIMESTAMP()", (student["id"],))
        db.execute("INSERT INTO qr_tokens(token_hash,student_id,user_id,expires_at) VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), student["id"], user["id"], expires))
    return {"token": token, "expires_at": utc_iso(expires), "server_time": datetime.now(timezone.utc).isoformat(), "ttl_seconds": config["qr_ttl_seconds"], "refresh_seconds": config["qr_refresh_seconds"], "session": session}


@router.get("/current-session")
def current_session(user=Depends(require_auth)):
    with get_db() as db:
        hostel=user.get("hostel_scope") or "*"
        if user["role"]=="student":
            hostel=authorize_student(db,user,user["student_id"])["hostel"]
        config=settings(db)
        session=active_session(db,hostel)
    return {"session":session,"meal":session["meal"] if session else None,"server_time":local_now(config).isoformat()}


@router.get("/scanner/status")
def scanner_status(user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    if not user["scanner_enabled"]:
        failure("UNAUTHORIZED_SCANNER", "Scanning permission is disabled", 403)
    with get_db() as db:
        config = settings(db)
        now = local_now(config)
        sessions = sessions_for(db, now.date(), config)
        session = active_session(db, user.get("hostel_scope") or "*")
        if user["role"]=="admin":
            sessions=[item for item in sessions if item["hostel"] in {"*",user.get("hostel_scope")}]
    return {"meal": session["meal"] if session else None, "session": session, "sessions": sessions, "server_time": now.isoformat(), "timezone": config["timezone"]}


@router.post("/scanner/heartbeat")
def heartbeat(mode: str = "hardware", user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    if not user["scanner_enabled"] or mode not in {"hardware", "camera"}:
        raise HTTPException(403, "Scanner is not authorized")
    with get_db() as db:
        db.execute("INSERT INTO scanner_devices(user_id,mode,last_seen) VALUES (?,?,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE mode=VALUES(mode),last_seen=UTC_TIMESTAMP()", (user["id"], mode))
    return {"status": "online"}


@router.post("/scanner/consume")
def consume_qr(payload: QRConsumeRequest, user=Depends(require_auth)):
    if user["role"] not in {"admin", "super-admin"} or not user["scanner_enabled"]:
        failure("UNAUTHORIZED_SCANNER", "Scanning permission is disabled", 403)
    rate_limit(f"scanner:{user['id']}", 180, 60)
    outcome = "SUCCESS"
    student_id = None
    student = session = token = None
    try:
        with get_db() as db:
            token = db.execute("SELECT * FROM qr_tokens WHERE token_hash=?", (hashlib.sha256(payload.token.encode()).hexdigest(),)).fetchone()
            if not token:
                failure("QR_INVALID", "This QR code could not be verified")
            try:
                student = authorize_student(db, user, token["student_id"], lock=True)
            except HTTPException as exc:
                if exc.status_code == 404:
                    failure("UNAUTHORIZED_SCANNER", "This scanner cannot check in students outside its authorized hostels", 404)
                raise
            student_id = student["id"]
            token = db.execute("SELECT * FROM qr_tokens WHERE id=? FOR UPDATE", (token["id"],)).fetchone()
            if student["approval_status"] == "SUSPENDED" or student["deleted_at"]:
                failure("STUDENT_SUSPENDED", "This student account is suspended. Contact an administrator before scanning again.", 403)
            if student["approval_status"] != "APPROVED":
                failure("STUDENT_NOT_APPROVED", "This student account has not been approved. Ask an administrator to review the registration.", 403)
            if not student["active"]:
                failure("STUDENT_SUSPENDED", "This student account is suspended. Contact an administrator before scanning again.", 403)
            if not student["qr_enabled"]:
                failure("STUDENT_INACTIVE", "QR access is disabled for this student", 403)
            if not db.execute("SELECT id FROM users WHERE id=? AND student_id=? AND role='student' AND active=1 FOR UPDATE", (token["user_id"],student_id)).fetchone():
                failure("STUDENT_INACTIVE", "The linked student account is inactive", 403)
            if not db.execute("SELECT id FROM hostels WHERE name=? AND active=1", (student["hostel"],)).fetchone():
                failure("NO_ACTIVE_SESSION", "This hostel is not accepting attendance", 422)
            if token["used_at"]:
                # A consumed token can explain a current-meal duplicate but can
                # never authorize another insert, including in a later meal.
                session = active_session(db, student["hostel"])
                if session and (not payload.meal or payload.meal == session["meal"]):
                    existing = existing_meal_attendance(db, student_id, session)
                    if existing and existing["qr_token_id"] == token["id"]:
                        duplicate_attendance(student, session, existing, replay=True)
                failure("QR_USED", "This QR code has already been used. Ask the student to show a fresh live QR.", 409)
            if token["expires_at"] <= datetime.now(timezone.utc).replace(tzinfo=None):
                failure("QR_EXPIRED", "Ask the student to refresh their live QR", 410)
            session = active_session(db, student["hostel"])
            if not session or (payload.meal and payload.meal != session["meal"]):
                failure("NO_ACTIVE_SESSION", "No authorized attendance session is currently open", 422)
            existing = existing_meal_attendance(db, student_id, session)
            if existing:
                duplicate_attendance(student, session, existing)
            cursor = db.execute("INSERT INTO meal_attendance(student_id,meal_date,meal_type,scanned_by,qr_token_id,session_id,hostel_at_scan) VALUES (?,?,?,?,?,?,?)", (student_id, session["session_date"], session["meal"], user["id"], token["id"], session["id"], student["hostel"]))
            db.execute("UPDATE qr_tokens SET used_at=UTC_TIMESTAMP() WHERE id=?", (token["id"],))
            repository.audit(user["id"], "attendance_marked", "meal_attendance", cursor.lastrowid, {"student_id": student_id, "session_id": session["id"]}, db=db)
            notify(db, token["user_id"], f"{session['meal']} attendance marked", f"Your {session['meal'].lower()} check-in has been recorded.", "attendance")
            attendance = db.execute("SELECT scanned_at FROM meal_attendance WHERE id=?", (cursor.lastrowid,)).fetchone()
        marked_at = utc_iso(attendance["scanned_at"])
        local_marked_at = datetime.fromisoformat(marked_at).astimezone(ZoneInfo(session["timezone"]))
        return {"success": True, "code": "MARKED", "status": "MARKED", "attendance_id": cursor.lastrowid, "student": scanner_student(student), "meal": session["meal"], "scan_date": session["session_date"], "scan_time": local_marked_at.strftime("%H:%M:%S"), "scanned_at": marked_at}
    except HTTPException as exc:
        outcome = exc.detail.get("code", "REJECTED") if isinstance(exc.detail, dict) else "REJECTED"
        raise
    except pymysql.IntegrityError as exc:
        # The database remains the final guard against simultaneous scanners.
        # Only a genuine duplicate key with a matching persisted attendance is
        # a duplicate result; foreign-key and unrelated integrity errors are not.
        outcome = "SERVER_ERROR"
        if exc.args and exc.args[0] == 1062 and student_id and session:
            with get_db() as db:
                student = authorize_student(db, user, student_id, lock=True)
                existing = existing_meal_attendance(db, student_id, session)
                if existing:
                    outcome = "ALREADY_MARKED"
                    duplicate_attendance(student, session, existing,
                                         replay=bool(token and existing["qr_token_id"] == token["id"]))
        raise
    except Exception:
        outcome = "SERVER_ERROR"
        raise
    finally:
        with get_db() as db:
            db.execute("INSERT INTO scan_events(user_id,student_id,outcome) VALUES (?,?,?)", (user["id"], student_id, outcome))
            if outcome in {"QR_INVALID","QR_USED","STUDENT_INACTIVE","STUDENT_NOT_APPROVED","STUDENT_SUSPENDED"}:
                notify(db,user["id"],"QR security notice",f"Your scanner rejected a check-in: {outcome.replace('_',' ').lower()}.","security")


@router.get("/student/profile-change-requests")
def my_requests(user=Depends(require_auth)):
    require_roles(user, {"student"})
    with get_db() as db:
        rows = db.execute("SELECT * FROM profile_change_requests WHERE student_id=? ORDER BY created_at DESC", (user["student_id"],)).fetchall()
    return {"requests": [{**r, "changes": json.loads(r["changes_json"]), "original": json.loads(r["original_json"] or "{}")} for r in rows]}


@router.post("/student/profile-change-requests")
def request_change(payload: ProfileChangeCreate, user=Depends(require_auth)):
    require_roles(user, {"student"})
    if set(payload.changes) - PROFILE_FIELDS or not payload.changes or any(v is not None and len(v) > 2000 for v in payload.changes.values()):
        raise HTTPException(422, "Only editable personal and academic fields can be requested")
    if payload.changes.get("name") is not None and len(payload.changes["name"].strip()) < 2:
        raise HTTPException(422, "Name must contain at least two characters")
    with get_db() as db:
        student = authorize_student(db, user, user["student_id"], lock=True)
        validation_fields=set(StudentCreateRequest.model_fields)-{"login_id","password"}
        try:
            StudentCreateRequest(**{key:payload.changes.get(key,student.get(key)) for key in validation_fields})
        except ValidationError:
            raise HTTPException(422,"Some requested values are invalid. Check field lengths and the photo URL.")
        if db.execute("SELECT id FROM profile_change_requests WHERE student_id=? AND status='Pending Approval'", (student["id"],)).fetchone():
            raise HTTPException(409, "A profile request is already pending")
        changes = {k: v for k, v in payload.changes.items() if v != student.get(k)}
        if not changes:
            raise HTTPException(422, "Change at least one field")
        cursor = db.execute("INSERT INTO profile_change_requests(student_id,requested_by,changes_json,original_json) VALUES (?,?,?,?)", (student["id"], user["id"], json.dumps(changes), json.dumps({k: student.get(k) for k in changes})))
        repository.audit(user["id"], "profile_change_requested", "profile_change_request", cursor.lastrowid, db=db)
    return {"id": cursor.lastrowid, "status": "Pending Approval"}


@router.get("/admin/profile-change-requests")
def profile_requests(user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    condition, args = scope(user)
    with get_db() as db:
        rows = db.execute(f"SELECT r.*,s.name,s.registration_number,s.hostel FROM profile_change_requests r JOIN students s ON s.id=r.student_id WHERE {condition} ORDER BY r.created_at DESC", args).fetchall()
    return {"requests": [{**r, "changes": json.loads(r["changes_json"]), "original": json.loads(r["original_json"] or "{}")} for r in rows]}


@router.post("/admin/profile-change-requests/{request_id}/decision")
def decide_request(request_id: int, payload: ProfileChangeDecision, user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    if payload.decision == "Rejected" and not payload.admin_note:
        raise HTTPException(422, "Please provide a rejection reason")
    with get_db() as db:
        req = db.execute("SELECT * FROM profile_change_requests WHERE id=? FOR UPDATE", (request_id,)).fetchone()
        if not req or req["status"] != "Pending Approval":
            raise HTTPException(409, "Request is missing or already reviewed")
        student = authorize_student(db, user, req["student_id"], lock=True)
        changes = {k: v for k, v in json.loads(req["changes_json"]).items() if k in PROFILE_FIELDS}
        if payload.decision == "Approved" and changes:
            original = json.loads(req["original_json"] or "{}")
            if any(student.get(k) != v for k, v in original.items()):
                raise HTTPException(409, "Student details changed since this request. Reject and request a new update.")
            db.execute(f"UPDATE students SET {','.join(f'{key}=?' for key in changes)} WHERE id=?", [*changes.values(), req["student_id"]])
            account_changes={k:v for k,v in changes.items() if k in {"name","email","mobile","photo_url"}}
            if account_changes:
                db.execute(f"UPDATE users SET {','.join(f'{key}=?' for key in account_changes)} WHERE student_id=?",[*account_changes.values(),req["student_id"]])
        db.execute("UPDATE profile_change_requests SET status=?,reviewed_by=?,admin_note=?,reviewed_at=UTC_TIMESTAMP() WHERE id=?", (payload.decision, user["id"], payload.admin_note, request_id))
        notify(db, req["requested_by"], f"Profile request {payload.decision.lower()}", payload.admin_note or "Your profile changes have been approved.", "profile")
        repository.audit(user["id"], f"profile_change_{payload.decision.lower()}", "profile_change_request", request_id, db=db)
    return {"status": payload.decision}


@router.get("/meal-menus")
def menus(week: date | None = None, user=Depends(require_auth)):
    today = week or local_now().date()
    start = today - timedelta(days=today.weekday())
    with get_db() as db:
        rows = db.execute("SELECT * FROM meal_menus WHERE menu_date BETWEEN ? AND ? ORDER BY menu_date", (start, start+timedelta(days=6))).fetchall()
    return {"week_start": start, "menus": rows}


@router.put("/admin/meal-menus")
def save_menu(payload: MealMenuUpsert, user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    with get_db() as db:
        db.execute("INSERT INTO meal_menus(menu_date,breakfast,lunch,snacks,dinner,updated_by) VALUES (?,?,?,?,?,?) ON DUPLICATE KEY UPDATE breakfast=VALUES(breakfast),lunch=VALUES(lunch),snacks=VALUES(snacks),dinner=VALUES(dinner),updated_by=VALUES(updated_by)", (payload.menu_date, payload.breakfast, payload.lunch, payload.snacks, payload.dinner, user["id"]))
        notify(db, None, "Meal menu updated", f"The menu for {payload.menu_date} is ready to view.", "menu")
        repository.audit(user["id"], "meal_menu_updated", "meal_menu", payload.menu_date, db=db)
    return {"status": "saved"}
