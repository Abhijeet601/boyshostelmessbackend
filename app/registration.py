"""Enrollment validation shared by public applications and staff account writers."""
import base64
import io
import secrets
import warnings

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from .security import create_token
from .services import notify

APPLICATION_FIELDS = "id,reference,admission_number,registration_number,login_id,name,email,mobile,hostel,room_number,course,department,academic_year,semester,academic_session,photo_url,status,rejection_reason,student_id,user_id,reviewed_by,reviewed_at,created_at,updated_at"


def status_credential(application):
    return create_token({"purpose": "registration-status", "reference": application["reference"]})


def notify_reviewers(db, name, hostel, resubmitted=False):
    reviewers = db.execute("SELECT id FROM users WHERE active=1 AND (role='super-admin' OR (role='admin' AND hostel_scope=?))", (hostel,)).fetchall()
    for reviewer in reviewers:
        notify(db, reviewer["id"], "Application resubmitted" if resubmitted else "New student application", f"{name} submitted a registration for review.", "registration")


def check_identifiers(db, admission=None, registration=None, login=None, exclude_application=0, student_id=0, user_id=0):
    for field, value, table, excluded in (("admission_number", admission, "students", student_id), ("registration_number", registration, "students", student_id), ("login_id", login, "users", user_id)):
        if not value:
            continue
        if db.execute(f"SELECT id FROM {table} WHERE {field}=? AND id<>?", (value, excluded)).fetchone() or db.execute(f"SELECT id FROM registration_applications WHERE {field}=? AND id<>?", (value, exclude_application)).fetchone():
            raise HTTPException(409, f"This {field.replace('_', ' ')} is already registered or reserved by an application.")


def save_photo(db, application_id, data):
    if not data:
        return None
    try:
        header, encoded = data.split(",", 1)
        if header not in {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 3 * 1024 * 1024:
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 16000000:
                    raise ValueError()
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((800, 800))
                output = io.BytesIO()
                image.save(output, "JPEG", quality=85)
        photo_id = secrets.token_hex(24)
        db.execute("INSERT INTO registration_photos(id,application_id,image_base64) VALUES (?,?,?)", (photo_id, application_id, base64.b64encode(output.getvalue()).decode("ascii")))
        return f"/api/registration/photos/{photo_id}"
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "Upload a valid JPEG, PNG or WebP photograph, at most 3 MB and 16 megapixels.")
