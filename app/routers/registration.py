import base64
import secrets
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from .. import repository
from ..db import enrollment_db, get_db
from ..registration import APPLICATION_FIELDS, check_identifiers, save_photo, status_credential, notify_reviewers
from ..schemas import AccountState, RegistrationDecision, RegistrationRequest
from ..security import decode_token, hash_password, require_auth, require_roles
from ..services import authorize_student, notify, rate_limit, require_hostel, scope, settings

router = APIRouter(tags=["student registration"])


def restricted(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to check your application status")
    claims = decode_token(authorization[7:])
    if claims.get("purpose") != "registration-status" or not claims.get("reference"):
        raise HTTPException(403, "An application status credential is required")
    with get_db() as db:
        row = db.execute(f"SELECT {APPLICATION_FIELDS} FROM registration_applications WHERE reference=?", (claims["reference"],)).fetchone()
    if not row:
        raise HTTPException(404, "Application not found")
    return row


@router.get("/public/registration")
def options():
    with get_db() as db:
        config = settings(db)
        hostels = db.execute("SELECT name FROM hostels WHERE active=1 ORDER BY name").fetchall()
    return {"hostels": [h["name"] for h in hostels], "academic_session": config["academic_session"], "allow_resubmission": config["registration_resubmission"]}


def application_values(payload):
    return payload.model_dump(exclude={"password", "confirm_password", "photo_data"})


@router.post("/public/registration", status_code=201)
def submit(payload: RegistrationRequest, request: Request):
    rate_limit(f"registration:{request.client.host}", 12, 3600)
    with enrollment_db() as db:
        require_hostel(db, {"role": "super-admin"}, payload.hostel)
        check_identifiers(db, payload.admission_number, payload.registration_number, payload.login_id)
        values = {**application_values(payload), "reference": "SM-" + secrets.token_hex(8).upper(), "password_hash": hash_password(payload.password)}
        application_id = db.execute(f"INSERT INTO registration_applications({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())).lastrowid
        photo = save_photo(db, application_id, payload.photo_data)
        db.execute("UPDATE registration_applications SET photo_url=? WHERE id=?", (photo, application_id))
        repository.audit(None, "registration_submitted", "registration", application_id, {"reference": values["reference"], "hostel": payload.hostel}, db=db)
        notify_reviewers(db, payload.name, payload.hostel)
    return {"reference": values["reference"], "status": "PENDING", "credential": status_credential(values)}


@router.get("/registration/status")
def status(application=Depends(restricted)):
    with get_db() as db:
        if application["student_id"]:
            student = db.execute("SELECT s.active,s.approval_status,u.active user_active FROM students s LEFT JOIN users u ON u.id=? WHERE s.id=?", (application["user_id"],application["student_id"])).fetchone()
            if student and (not student["active"] or not student["user_active"] or student["approval_status"] == "SUSPENDED"):
                application["status"] = "SUSPENDED"
        history = db.execute("SELECT action,details,created_at FROM audit_logs WHERE entity_type='registration' AND entity_id=? ORDER BY id", (str(application["id"]),)).fetchall()
        return {**application, "allow_resubmission": settings(db)["registration_resubmission"], "history": history}


@router.put("/registration/resubmit")
def resubmit(payload: RegistrationRequest, application=Depends(restricted)):
    rate_limit(f"resubmit:{application['id']}", 6, 3600)
    with enrollment_db() as db:
        current = db.execute("SELECT * FROM registration_applications WHERE id=? FOR UPDATE", (application["id"],)).fetchone()
        if current["status"] != "REJECTED" or not settings(db)["registration_resubmission"]:
            raise HTTPException(409, "This application cannot be resubmitted")
        require_hostel(db, {"role": "super-admin"}, payload.hostel)
        check_identifiers(db, payload.admission_number, payload.registration_number, payload.login_id, current["id"])
        values = {**application_values(payload), "password_hash": hash_password(payload.password)}
        if payload.photo_data:
            values["photo_url"] = save_photo(db, current["id"], payload.photo_data)
        db.execute(f"UPDATE registration_applications SET {','.join(k+'=?' for k in values)},status='PENDING',rejection_reason=NULL,reviewed_by=NULL,reviewed_at=NULL WHERE id=?", [*values.values(), current["id"]])
        changes = {key:{"before":current.get(key),"after":value} for key,value in values.items() if key not in {"password_hash","photo_url"} and current.get(key)!=value}
        repository.audit(None, "registration_resubmitted", "registration", current["id"], {"changes": changes}, db=db)
        notify_reviewers(db, payload.name, payload.hostel, resubmitted=True)
    return {"reference": current["reference"], "status": "PENDING", "credential": status_credential(current)}


@router.get("/registration/photos/{photo_id}")
def photo(photo_id: str, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    claims = decode_token(authorization[7:])
    with get_db() as db:
        row = db.execute("SELECT p.image_base64,a.reference,a.hostel,a.student_id FROM registration_photos p JOIN registration_applications a ON a.id=p.application_id WHERE p.id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Photograph not found")
    if claims.get("purpose") == "registration-status":
        if claims.get("reference") != row["reference"]:
            raise HTTPException(404, "Photograph not found")
    else:
        user = require_auth(authorization)
        if user["role"] == "student" and user["student_id"] != row["student_id"] or user["role"] == "admin" and user.get("hostel_scope") != row["hostel"]:
            raise HTTPException(404, "Photograph not found")
    return Response(base64.b64decode(row["image_base64"]), media_type="image/jpeg")


@router.get("/registration/requests")
def requests(q: str = "", hostel: str = "", status: str = "", from_date: date | None = None, to_date: date | None = None, page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100), user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    condition, scoped_args = scope(user, "a")
    where, args = [condition], list(scoped_args)
    if q:
        where.append("(a.name LIKE ? OR a.admission_number LIKE ? OR a.registration_number LIKE ? OR a.reference LIKE ?)")
        args += [f"%{q}%"] * 4
    for key, value in (("hostel", hostel), ("status", status)):
        if value:
            where.append(f"a.{key}=?")
            args.append(value)
    for operator, value in ((">=", from_date), ("<=", to_date)):
        if value:
            where.append(f"DATE(a.created_at){operator}?")
            args.append(value)
    with get_db() as db:
        counts = db.execute(f"SELECT status,COUNT(*) n FROM registration_applications a WHERE {condition} GROUP BY status", scoped_args).fetchall()
        total = db.execute(f"SELECT COUNT(*) n FROM registration_applications a WHERE {' AND '.join(where)}", args).fetchone()["n"]
        items = db.execute(f"SELECT {','.join('a.'+field for field in APPLICATION_FIELDS.split(','))} FROM registration_applications a WHERE {' AND '.join(where)} ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?", [*args, page_size, (page-1)*page_size]).fetchall()
        trend = db.execute(f"SELECT DATE(reviewed_at) date,COUNT(*) value FROM registration_applications a WHERE {condition} AND status='APPROVED' AND reviewed_at>=UTC_TIMESTAMP()-INTERVAL 30 DAY GROUP BY DATE(reviewed_at) ORDER BY date", scoped_args).fetchall()
    summary = {state: 0 for state in ("PENDING", "APPROVED", "REJECTED")}
    summary.update({row["status"]: row["n"] for row in counts})
    return {"items": items, "total": total, "page": page, "page_size": page_size, "summary": {**summary, "TOTAL": sum(r["n"] for r in counts)}, "trend": trend}


@router.get("/registration/requests/{application_id}")
def detail(application_id: int, user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    condition, args = scope(user, "a")
    with get_db() as db:
        row = db.execute(f"SELECT {','.join('a.'+f for f in APPLICATION_FIELDS.split(','))} FROM registration_applications a WHERE a.id=? AND {condition}", [application_id, *args]).fetchone()
        if not row:
            raise HTTPException(404, "Application not found in your authorized hostels")
        history = db.execute("SELECT l.action,l.details,l.created_at,u.name reviewer FROM audit_logs l LEFT JOIN users u ON u.id=l.actor_user_id WHERE l.entity_type='registration' AND l.entity_id=? ORDER BY l.id DESC", (str(application_id),)).fetchall()
        conflicts = []
        try:
            check_identifiers(db, row["admission_number"], row["registration_number"], row["login_id"], row["id"], row["student_id"] or 0, row["user_id"] or 0)
        except HTTPException as exc:
            conflicts.append(exc.detail)
    return {**row, "history": history, "conflicts": conflicts}


@router.post("/registration/requests/{application_id}/decision")
def decide(application_id: int, payload: RegistrationDecision, user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    if payload.decision == "REJECTED" and not payload.reason.strip():
        raise HTTPException(422, "A rejection reason is required")
    with enrollment_db() as db:
        condition, args = scope(user, "a")
        app = db.execute(f"SELECT a.* FROM registration_applications a WHERE a.id=? AND {condition} FOR UPDATE", [application_id, *args]).fetchone()
        if not app:
            raise HTTPException(404, "Application not found in your authorized hostels")
        if app["status"] == payload.decision:
            return {"status": app["status"], "student_id": app["student_id"], "already_reviewed": True}
        if app["status"] != "PENDING":
            raise HTTPException(409, "This application has already been reviewed")
        require_hostel(db, user, app["hostel"])
        if payload.decision == "APPROVED":
            check_identifiers(db, app["admission_number"], app["registration_number"], app["login_id"], app["id"])
            fields = "admission_number,registration_number,name,email,mobile,hostel,room_number,course,department,academic_year,semester,academic_session,photo_url".split(",")
            student_id = db.execute(f"INSERT INTO students({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", [app[f] for f in fields]).lastrowid
            user_id = db.execute("INSERT INTO users(login_id,password_hash,role,name,email,mobile,photo_url,student_id,active) VALUES (?,?,'student',?,?,?,?,?,1)", (app["login_id"], app["password_hash"], app["name"], app["email"], app["mobile"], app["photo_url"], student_id)).lastrowid
            db.execute("UPDATE registration_applications SET student_id=?,user_id=? WHERE id=?", (student_id, user_id, app["id"]))
            notify(db, user_id, "Your account is approved", "Welcome! Your student account and secure QR are ready.", "registration")
        else:
            student_id = None
        db.execute("UPDATE registration_applications SET status=?,rejection_reason=?,reviewed_by=?,reviewed_at=UTC_TIMESTAMP() WHERE id=?", (payload.decision, payload.reason.strip() if payload.decision == "REJECTED" else None, user["id"], app["id"]))
        repository.audit(user["id"], "registration_" + payload.decision.lower(), "registration", app["id"], {"reason": payload.reason.strip(), "student_id": student_id}, db=db)
    return {"status": payload.decision, "student_id": student_id}


@router.post("/students/{student_id}/account-state")
def account_state(student_id: int, payload: AccountState, user=Depends(require_auth)):
    require_roles(user, {"admin", "super-admin"})
    with get_db() as db:
        student = authorize_student(db, user, student_id, lock=True)
        if student["deleted_at"]:
            raise HTTPException(409, "Archived students cannot be reactivated")
        if student["approval_status"] not in {"APPROVED", "SUSPENDED"}:
            raise HTTPException(409, "Approval is required before activation")
        state = "APPROVED" if payload.active else "SUSPENDED"
        db.execute("UPDATE students SET active=?,approval_status=? WHERE id=?", (payload.active, state, student_id))
        db.execute("UPDATE users SET active=?,session_version=session_version+1 WHERE student_id=?", (payload.active, student_id))
        repository.audit(user["id"], "student_reactivated" if payload.active else "student_suspended", "student", student_id, {"reason": payload.reason}, db=db)
        app = db.execute("SELECT id,user_id FROM registration_applications WHERE student_id=?", (student_id,)).fetchone()
        if app:
            repository.audit(user["id"], "account_" + state.lower(), "registration", app["id"], {"reason": payload.reason}, db=db)
            notify(db, app["user_id"], "Account status updated", f"Your account is {state.lower()}. {payload.reason}", "registration")
    return {"status": state}
