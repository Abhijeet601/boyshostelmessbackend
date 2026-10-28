import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from .db import get_db

DEFAULT_SETTINGS = {
    "app_name": "SmartMess", "institution_name": "", "logo_url": "", "contact_email": "", "contact_phone": "",
    "academic_session": "", "timezone": "Asia/Kolkata", "qr_ttl_seconds": 30, "qr_refresh_seconds": 25,
    "scanner_sound": True, "accent": "indigo", "registration_resubmission": True,
    "schedules": [
        {"meal": "Breakfast", "start": "07:30", "end": "09:30", "enabled": True},
        {"meal": "Lunch", "start": "12:30", "end": "14:30", "enabled": True},
        {"meal": "Snacks", "start": "16:00", "end": "18:00", "enabled": False},
        {"meal": "Dinner", "start": "19:30", "end": "21:30", "enabled": True},
    ],
}


def settings(db=None):
    if db is None:
        with get_db() as connection:
            return settings(connection)
    row = db.execute("SELECT value_json FROM app_settings WHERE id=1").fetchone()
    return {**DEFAULT_SETTINGS, **(json.loads(row["value_json"]) if row else {})}


def local_now(config=None):
    return datetime.now(ZoneInfo((config or settings())["timezone"]))


def validate_settings(data):
    allowed = set(DEFAULT_SETTINGS)
    if set(data) - allowed:
        raise HTTPException(422, "Unknown settings field")
    result = {**DEFAULT_SETTINGS, **data}
    if not isinstance(result["accent"],str) or result["accent"] not in {"indigo", "violet", "blue"}:
        raise HTTPException(422, "Unsupported accent")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise HTTPException(422, "Choose a valid IANA timezone")
    try:
        ttl,refresh=int(result["qr_ttl_seconds"]),int(result["qr_refresh_seconds"])
    except (TypeError,ValueError,OverflowError):
        raise HTTPException(422,"QR expiry and refresh must be whole seconds")
    if result["qr_ttl_seconds"]!=ttl or result["qr_refresh_seconds"]!=refresh or not 15 <= ttl <= 120 or not 5 <= refresh < ttl:
        raise HTTPException(422, "QR refresh must be shorter than expiry (15–120 seconds)")
    result["qr_ttl_seconds"],result["qr_refresh_seconds"]=ttl,refresh
    if not isinstance(result["scanner_sound"],bool):
        raise HTTPException(422,"Scanner sound must be enabled or disabled")
    if not isinstance(result["registration_resubmission"], bool):
        raise HTTPException(422, "Registration resubmission must be enabled or disabled")
    import re
    schedules = result["schedules"]
    if not isinstance(schedules, list) or len(schedules) != 4 or not all(isinstance(s,dict) and isinstance(s.get("meal"),str) and isinstance(s.get("enabled"),bool) for s in schedules) or {s.get("meal") for s in schedules} != {"Breakfast", "Lunch", "Snacks", "Dinner"}:
        raise HTTPException(422, "Provide one schedule for each meal")
    for item in schedules:
        if any(not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(item.get(key, ""))) for key in ("start", "end")) or item["start"] == item["end"]:
            raise HTTPException(422, "Meal schedules need distinct valid start and end times")
    for index, item in enumerate(schedules):
        if not item["enabled"]:
            continue
        start = int(item["start"][:2])*60+int(item["start"][3:])
        end = int(item["end"][:2])*60+int(item["end"][3:])
        end += 1440 if end <= start else 0
        for other in schedules[index+1:]:
            if not other["enabled"]:
                continue
            a = int(other["start"][:2])*60+int(other["start"][3:])
            b = int(other["end"][:2])*60+int(other["end"][3:])
            b += 1440 if b <= a else 0
            if any(max(start,a+shift)<min(end,b+shift) for shift in (-1440,0,1440)):
                raise HTTPException(422, "Enabled meal schedules must not overlap")
    for key in ("app_name", "institution_name", "logo_url", "contact_email", "contact_phone", "academic_session"):
        if not isinstance(result[key], str) or len(result[key]) > 2000:
            raise HTTPException(422, "Invalid branding value")
    if result["logo_url"] and not result["logo_url"].startswith(("https://", "http://", "/")):
        raise HTTPException(422, "Logo must be an HTTP(S) URL")
    return result


def scope(user, alias="s"):
    prefix = f"{alias}." if alias else ""
    if user["role"] == "student":
        return f"{prefix}id=?", [user.get("student_id")]
    if user["role"] == "admin" and user.get("hostel_scope"):
        return f"{prefix}hostel=?", [user["hostel_scope"]]
    if user["role"] == "admin":
        return "1=0", []
    return "1=1", []


def authorize_student(db, user, student_id, lock=False):
    condition, args = scope(user)
    row = db.execute(f"SELECT s.* FROM students s WHERE s.id=? AND {condition}" + (" FOR UPDATE" if lock else ""), [student_id, *args]).fetchone()
    if not row:
        raise HTTPException(404, "Student not found in your authorized hostels")
    return row


def require_hostel(db, user, hostel):
    if user["role"] == "admin" and user.get("hostel_scope") != hostel:
        raise HTTPException(403, "This hostel is outside your permissions")
    if not db.execute("SELECT id FROM hostels WHERE name=? AND active=1", (hostel,)).fetchone():
        raise HTTPException(422, "Select an active hostel")


def sessions_for(db, day, config=None):
    config = config or settings(db)
    for item in config["schedules"]:
        db.execute("INSERT INTO attendance_sessions(meal,session_date,start_time,end_time,hostel,active,timezone) VALUES (?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=id",
                   (item["meal"], day, item["start"], item["end"], "*", int(item["enabled"]), config["timezone"]))
    # A locking read sees rows inserted by a concurrent initializer even when this
    # transaction's earlier settings read established an older InnoDB snapshot.
    return list(db.execute("SELECT * FROM attendance_sessions WHERE session_date=? ORDER BY start_time FOR UPDATE", (day,)).fetchall())


def session_bounds(session):
    tz = ZoneInfo(session["timezone"])
    start = datetime.fromisoformat(f"{session['session_date']}T{session['start_time']}").replace(tzinfo=tz)
    end = datetime.fromisoformat(f"{session['session_date']}T{session['end_time']}").replace(tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def active_session(db, hostel, meal=None):
    config = settings(db)
    now = local_now(config)
    all_sessions = sessions_for(db, now.date(), config) + sessions_for(db, now.date() - timedelta(days=1), config)
    # Hostel overrides, including disabled overrides, take precedence over global schedules.
    overrides = {(s["session_date"], s["meal"]) for s in all_sessions if hostel != "*" and s["hostel"] == hostel}
    for session in sorted(all_sessions, key=lambda s: s["hostel"] == "*"):
        if session["hostel"] not in ("*", hostel) or (session["hostel"] == "*" and (session["session_date"], session["meal"]) in overrides):
            continue
        start, end = session_bounds(session)
        if session["active"] and (not meal or session["meal"] == meal) and start <= now < end:
            return session
    return None


def notify(db, user_id, title, body, kind="system", hostel=None):
    db.execute("INSERT INTO notifications(user_id,title,body,kind,hostel) VALUES (?,?,?,?,?)", (user_id, title, body, kind, hostel))


def utc_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.replace(tzinfo=timezone.utc).isoformat()


def attendance_percentages(db, students):
    """Batch 30-day rates using effective hostel overrides and enrollment time."""
    if not students:
        return {}
    config = settings(db)
    now = local_now(config)
    start, end = now.date()-timedelta(days=29), now.date()
    ids = [s["id"] for s in students]
    rows = db.execute(f"SELECT student_id,meal_date,meal_type FROM meal_attendance WHERE student_id IN ({','.join('?' for _ in ids)}) AND meal_date BETWEEN ? AND ?", [*ids,start,end]).fetchall()
    recorded = {(r["student_id"],r["meal_date"],r["meal_type"]) for r in rows}
    sessions = db.execute("SELECT * FROM attendance_sessions WHERE session_date BETWEEN ? AND ?", (start,end)).fetchall()
    by_key = {(s["session_date"],s["meal"],s["hostel"]):s for s in sessions}
    result = {}
    for student in students:
        present = expected = 0
        for offset in range(30):
            day = start+timedelta(days=offset)
            for item in config["schedules"]:
                session = by_key.get((day,item["meal"],student["hostel"])) or by_key.get((day,item["meal"],"*")) or {"session_date":day,"start_time":item["start"],"end_time":item["end"],"active":item["enabled"],"timezone":config["timezone"]}
                begin,finish = session_bounds(session)
                if (student["id"],day,item["meal"]) in recorded:
                    present += 1
                    expected += 1
                elif session["active"] and begin<=now and student["created_at"].replace(tzinfo=timezone.utc)<finish:
                    expected += 1
        result[student["id"]] = round(present/expected*100,1) if expected else None
    return result


def validate_session_windows(db, config, candidate=None):
    """Validate effective global/hostel schedules, including overnight neighbors."""
    centers = {candidate.session_date} if candidate else {local_now(config).date()}
    if candidate is None:
        centers.update(r["session_date"] for r in db.execute("SELECT DISTINCT session_date FROM attendance_sessions WHERE custom=1 AND session_date>=?", (local_now(config).date()-timedelta(days=1),)).fetchall())
    days = {day+timedelta(days=delta) for day in centers for delta in (-1,0,1)}
    existing = db.execute("SELECT * FROM attendance_sessions WHERE session_date BETWEEN ? AND ? AND custom=1", (min(days),max(days))).fetchall()
    all_sessions = {(day,item["meal"],"*"): {"session_date":day,"meal":item["meal"],"hostel":"*","start_time":item["start"],"end_time":item["end"],"active":item["enabled"],"timezone":config["timezone"]} for day in days for item in config["schedules"]}
    for row in existing:
        if row["session_date"] in days:
            all_sessions[row["session_date"],row["meal"],row["hostel"]] = row
    if candidate:
        values = {**candidate.model_dump(),"timezone":config["timezone"]}
        all_sessions[candidate.session_date,candidate.meal,candidate.hostel] = values
    hostels = {"*"} | {r["name"] for r in db.execute("SELECT name FROM hostels WHERE active=1").fetchall()} | {key[2] for key in all_sessions}
    for hostel in hostels:
        effective = [all_sessions.get((day,item["meal"],hostel)) or all_sessions[(day,item["meal"],"*")] for day in days for item in config["schedules"]]
        windows = sorted(((*session_bounds(row),row["meal"]) for row in effective if row["active"]),key=lambda w:w[0])
        for previous,current in zip(windows,windows[1:]):
            if previous[1] > current[0]:
                raise HTTPException(422, f"{previous[2]} overlaps {current[2]} for {hostel}. Adjust session timings or disable the conflicting session.")


def rate_limit(key, limit, seconds):
    import hashlib
    import time
    bucket = int(time.time()) // seconds
    digest = hashlib.sha256(f"{key}:{bucket}".encode()).hexdigest()
    with get_db() as db:
        db.execute("INSERT INTO rate_limits(bucket_key,hits,expires_at) VALUES (?,1,?) ON DUPLICATE KEY UPDATE hits=hits+1", (digest, datetime.fromtimestamp((bucket+1)*seconds, timezone.utc).replace(tzinfo=None)))
        hits = db.execute("SELECT hits FROM rate_limits WHERE bucket_key=?", (digest,)).fetchone()["hits"]
    if hits > limit:
        raise HTTPException(429, "Too many requests. Please try again shortly.")
