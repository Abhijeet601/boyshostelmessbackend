import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Header, HTTPException, status

from .config import APP_SECRET, TOKEN_TTL_SECONDS


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
      algorithm, salt_b64, digest_b64 = encoded.split("$", 2)
    except ValueError:
      return False
    if algorithm != "pbkdf2_sha256":
      return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return hmac.compare_digest(actual, expected)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_token(payload: dict[str, Any]) -> str:
    body = {**payload, "jti": os.urandom(24).hex(), "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    body_b64 = _b64(raw)
    signature = hmac.new(APP_SECRET.encode(), body_b64.encode(), hashlib.sha256).digest()
    return f"{body_b64}.{_b64(signature)}"


def decode_token(token: str) -> dict[str, Any]:
    try:
        body_b64, sig_b64 = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    expected = hmac.new(APP_SECRET.encode(), body_b64.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _unb64(sig_b64)):
            raise ValueError("signature")
        body = json.loads(_unb64(body_b64))
        if not isinstance(body, dict) or not isinstance(body.get("exp"), int):
            raise ValueError("payload")
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(401, "Invalid token") from exc
    if int(body.get("exp", 0)) <= int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return body


def require_auth(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    token = authorization.removeprefix("Bearer ").strip()
    claims = decode_token(token)
    if claims.get("purpose"):
        raise HTTPException(403, "This credential cannot access the application")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    from .db import get_db
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=? AND active=1", (claims.get("user_id"),)).fetchone()
        revoked = db.execute("SELECT token_hash FROM revoked_sessions WHERE token_hash=?", (token_hash,)).fetchone()
    if revoked:
        raise HTTPException(401, "This session has been signed out. Please sign in again.")
    if not user or claims.get("role") != user["role"] or claims.get("version", 0) != user["session_version"]:
        raise HTTPException(401, "Your session has ended. Please sign in again.")
    if user["role"] == "student":
        with get_db() as db:
            student = db.execute("SELECT id FROM students WHERE id=? AND active=1 AND approval_status='APPROVED' AND deleted_at IS NULL", (user["student_id"],)).fetchone()
        if not student:
            raise HTTPException(401, "Student account is inactive or unlinked")
    user.pop("password_hash", None)
    return {**user, "user_id": user["id"], "_token_hash": token_hash, "_token_exp": claims["exp"]}


def require_roles(user: dict[str, Any], roles: set[str]) -> None:
    if user.get("role") not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
