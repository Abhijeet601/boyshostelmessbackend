from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import repository
from ..db import get_db
from ..schemas import LoginRequest, LoginResponse, PasswordChange, ProfileUpdate
from ..security import create_token, hash_password, require_auth, verify_password
from ..services import rate_limit
from ..registration import status_credential

router = APIRouter(prefix="/auth", tags=["authentication"])


def safe_user(user):
    return {key: value for key, value in user.items() if key not in ("password_hash", "session_version") and not key.startswith("_")}


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request):
    rate_limit(f"login-ip:{request.client.host}", 100, 300)
    rate_limit(f"login:{request.client.host}:{payload.login_id.lower()}", 12, 300)
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE login_id=?", (payload.login_id.strip(),)).fetchone()
        application = db.execute("SELECT reference,status,password_hash FROM registration_applications WHERE login_id=?", (payload.login_id.strip(),)).fetchone()
    if not user and application and verify_password(payload.password, application["password_hash"]):
        raise HTTPException(403, {"code": "REGISTRATION_"+application["status"], "message": "Your account is awaiting administrator approval." if application["status"] == "PENDING" else "Your registration has not been approved.", "credential": status_credential(application)})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(401, "Invalid login ID or password")
    if not user["active"]:
        raise HTTPException(403, {"code": "ACCOUNT_SUSPENDED", "message": "Your account is currently inactive. Please contact the administrator."})
    if user["role"] == "student" and not repository.get_student(user["student_id"]):
        raise HTTPException(403, "Student account is inactive or unlinked")
    token = create_token({"user_id": user["id"], "role": user["role"], "student_id": user["student_id"], "version": user["session_version"]})
    repository.audit(user["id"], "login", "user", user["id"])
    return {"token": token, "role": user["role"], "user": safe_user(user)}


@router.get("/me")
def me(user=Depends(require_auth)):
    return safe_user(user)


@router.get("/activity")
def activity(user=Depends(require_auth)):
    with get_db() as db:
        rows = db.execute("SELECT id,action,created_at FROM audit_logs WHERE actor_user_id=? AND entity_type IN ('student','user','hostel','session','settings','meal_attendance','profile_change_request','meal_menu','notification','backup','scanner','report','registration') ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
    return {"items": rows}


@router.post("/logout")
def logout(user=Depends(require_auth)):
    with get_db() as db:
        # Logout revokes this bearer session. Password/role/account changes still
        # invalidate all sessions through the account's session_version.
        expiry=datetime.fromtimestamp(user["_token_exp"],timezone.utc).replace(tzinfo=None)
        db.execute("INSERT INTO revoked_sessions(token_hash,user_id,expires_at) VALUES (?,?,?) ON DUPLICATE KEY UPDATE token_hash=VALUES(token_hash)", (user["_token_hash"],user["id"],expiry))
        repository.audit(user["id"],"logout","user",user["id"],db=db)
    return {"status": "signed_out"}


@router.patch("/profile")
def update_profile(payload: ProfileUpdate, user=Depends(require_auth)):
    if user["role"] == "student":
        raise HTTPException(403, "Submit a profile change request")
    with get_db() as db:
        db.execute("UPDATE users SET name=?,email=?,mobile=?,photo_url=? WHERE id=?", (payload.name, payload.email, payload.mobile, payload.photo_url, user["id"]))
        repository.audit(user["id"], "profile_updated", "user", user["id"], db=db)
    return {"status": "saved"}


@router.post("/password")
def change_password(payload: PasswordChange, user=Depends(require_auth)):
    rate_limit(f"password:{user['id']}", 8, 300)
    with get_db() as db:
        stored = db.execute("SELECT password_hash FROM users WHERE id=? FOR UPDATE", (user["id"],)).fetchone()
        if not verify_password(payload.current_password, stored["password_hash"]):
            raise HTTPException(400, "Current password is incorrect")
        db.execute("UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?", (hash_password(payload.new_password), user["id"]))
        repository.audit(user["id"], "password_changed", "user", user["id"], db=db)
    return {"status": "Password updated. Sign in again."}
