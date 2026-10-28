import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..db import get_db
from ..security import require_auth, require_roles
from ..services import local_now, scope, session_bounds, sessions_for, settings, utc_iso

router = APIRouter(tags=["attendance and analytics"])


def filters(user, start, end, q="", hostel="", meal="", course="", year="", student_id=None):
    if end < start or (end-start).days > 366:
        raise HTTPException(422, "Choose a date range of up to 366 days")
    condition, args = scope(user)
    where = [condition, "a.meal_date BETWEEN ? AND ?"]
    args += [start, end]
    if q:
        where.append("(s.name LIKE ? OR s.admission_number LIKE ? OR s.registration_number LIKE ?)")
        args += [f"%{q}%"] * 3
    for key, value in (("s.hostel", hostel), ("a.meal_type", meal), ("s.course", course), ("s.academic_year", year), ("s.id", student_id)):
        if value:
            where.append(f"{key}=?")
            args.append(value)
    return " AND ".join(where), args


def records(db, where, args, limit=100, offset=0, order="DESC"):
    rows = db.execute(f"SELECT a.*,s.name,s.admission_number,s.registration_number,s.photo_url,COALESCE(a.hostel_at_scan,s.hostel) hostel,s.room_number,u.name operator FROM meal_attendance a JOIN students s ON s.id=a.student_id LEFT JOIN users u ON u.id=a.scanned_by WHERE {where} ORDER BY a.scanned_at {order},a.id {order} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return [{**row, "scanned_at": utc_iso(row["scanned_at"]), "status": "Present"} for row in rows]


def missed_records(db,user,start,end,q="",hostel="",meal="",course="",year="",student_id=None,sort="newest"):
    config=settings(db)
    now=local_now(config)
    condition,args=scope(user)
    for key,value in (("hostel",hostel),("course",course),("academic_year",year),("id",student_id)):
        if value:
            condition+=f" AND s.{key}=?"
            args.append(value)
    if q:
        condition+=" AND (s.name LIKE ? OR s.admission_number LIKE ? OR s.registration_number LIKE ?)"
        args.extend([f"%{q}%"]*3)
    students=db.execute(f"SELECT s.* FROM students s WHERE {condition}",args).fetchall()
    if len(students)*((end-start).days+1)>100000:
        raise HTTPException(422,"Narrow the date range for missed-attendance reporting")
    where,params=filters(user,start,end,q,hostel,meal,course,year,student_id)
    attended={(r["student_id"],r["meal_date"],r["meal_type"]) for r in db.execute(f"SELECT a.student_id,a.meal_date,a.meal_type FROM meal_attendance a JOIN students s ON s.id=a.student_id WHERE {where}",params).fetchall()}
    sessions={(s["session_date"],s["meal"],s["hostel"]):s for s in db.execute("SELECT * FROM attendance_sessions WHERE session_date BETWEEN ? AND ?",(start,end)).fetchall()}
    result=[]
    cache={}
    for i in range((min(end,now.date())-start).days+1):
        day=start+timedelta(days=i)
        for student in students:
            if student["deleted_at"] and student["deleted_at"].date()<day:
                continue
            for schedule in config["schedules"]:
                if meal and meal!=schedule["meal"]:
                    continue
                key=(day,schedule["meal"],student["hostel"])
                if key not in cache:
                    session=sessions.get(key) or sessions.get((day,schedule["meal"],"*")) or {"session_date":day,"start_time":schedule["start"],"end_time":schedule["end"],"active":schedule["enabled"],"timezone":config["timezone"]}
                    cache[key]=(session["active"],session_bounds(session)[1])
                enabled,finish=cache[key]
                if not enabled or finish>now or finish<=student["created_at"].replace(tzinfo=timezone.utc) or (student["id"],day,schedule["meal"]) in attended:
                    continue
                result.append({"id":f"missed-{student['id']}-{day}-{schedule['meal']}","student_id":student["id"],"name":student["name"],"photo_url":student["photo_url"],"admission_number":student["admission_number"],"registration_number":student["registration_number"],"hostel":student["hostel"],"room_number":student["room_number"],"meal_type":schedule["meal"],"meal_date":day,"scanned_at":None,"status":"Missed","operator":None})
    result.sort(key=lambda r:(r["meal_date"],r["name"],r["meal_type"]),reverse=sort!="oldest")
    return result


@router.get("/attendance")
def attendance(from_date: date | None = None, to_date: date | None = None, q: str = "", hostel: str = "", meal: str = "", course: str = "", year: str = "", student_id: int | None = None, sort: str = "newest", status: str = "Present", page: int = Query(1, ge=1), page_size: int = Query(12, ge=1, le=100), user=Depends(require_auth)):
    today = local_now().date()
    start, end = from_date or today-timedelta(days=30), to_date or today
    where, args = filters(user, start, end, q, hostel, meal, course, year, student_id)
    if status not in {"Present","Missed"}:
        raise HTTPException(422,"Choose Present or Missed attendance")
    with get_db() as db:
        if status=="Missed":
            rows=missed_records(db,user,start,end,q,hostel,meal,course,year,student_id,sort)
            return {"items":rows[(page-1)*page_size:page*page_size],"total":len(rows),"page":page,"page_size":page_size}
        total = db.execute(f"SELECT COUNT(*) n FROM meal_attendance a JOIN students s ON s.id=a.student_id WHERE {where}", args).fetchone()["n"]
        items = records(db, where, args, page_size, (page-1)*page_size, "ASC" if sort == "oldest" else "DESC")
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/attendance/export")
def export_attendance(from_date: date | None = None, to_date: date | None = None, q: str = "", hostel: str = "", meal: str = "", course: str = "", year: str = "", student_id: int | None = None, status: str = "Present", format: str = "csv", user=Depends(require_auth)):
    today = local_now().date()
    where, args = filters(user, from_date or today-timedelta(days=30), to_date or today, q, hostel, meal, course, year, student_id)
    with get_db() as db:
        if status not in {"Present","Missed"}:
            raise HTTPException(422,"Choose Present or Missed attendance")
        rows = missed_records(db,user,from_date or today-timedelta(days=30),to_date or today,q,hostel,meal,course,year,student_id) if status=="Missed" else records(db, where, args, 100001)
    if len(rows) > 100000:
        raise HTTPException(422, "Narrow your export date range (maximum 100,000 rows)")
    columns = ["name", "admission_number", "registration_number", "hostel", "meal_type", "meal_date", "scanned_at", "status", "operator"]
    def safe(value):
        value = str(value or "")
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    values = [[safe(r.get(k)) for k in columns] for r in rows]
    if format == "xlsx":
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "Attendance"
        sheet.append(columns)
        for row in values:
            sheet.append(row)
        output = io.BytesIO()
        book.save(output)
        return Response(output.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="attendance.xlsx"'})
    if format != "csv":
        raise HTTPException(422, "Supported formats: csv, xlsx")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(values)
    return Response("\ufeff"+output.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="attendance.csv"'})


@router.get("/overview")
def overview(from_date: date | None = None, to_date: date | None = None, q: str = "", hostel: str = "", meal: str = "", course: str = "", year: str = "", student_id: int | None = None, user=Depends(require_auth)):
    config = settings()
    now = local_now(config)
    today = now.date()
    start, end = from_date or today-timedelta(days=29), to_date or today
    where, args = filters(user, start, end, q, hostel, meal, course, year, student_id)
    student_where, student_args = scope(user)
    if q:
        student_where += " AND (s.name LIKE ? OR s.admission_number LIKE ? OR s.registration_number LIKE ?)"
        student_args += [f"%{q}%"]*3
    for key, value in (("hostel", hostel), ("course", course), ("academic_year", year), ("id", student_id)):
        if value:
            student_where += f" AND s.{key}=?"
            student_args.append(value)
    with get_db() as db:
        students = db.execute(f"SELECT s.* FROM students s WHERE {student_where}", student_args).fetchall()
        rows = db.execute(f"SELECT a.student_id,a.meal_date,a.meal_type,a.scanned_at,COALESCE(a.hostel_at_scan,s.hostel) hostel,s.course,s.academic_year FROM meal_attendance a JOIN students s ON s.id=a.student_id WHERE {where}", args).fetchall()
        today_where, today_args = filters(user, today, today, q, hostel, meal, course, year, student_id)
        today_rows = db.execute(f"SELECT a.student_id,a.meal_type FROM meal_attendance a JOIN students s ON s.id=a.student_id WHERE {today_where}", today_args).fetchall()
        recent = records(db, where, args, 8)
        sessions = sessions_for(db, today, config)
        historical_sessions = db.execute("SELECT * FROM attendance_sessions WHERE session_date BETWEEN ? AND ?", (start, end)).fetchall()
        student_profile = students[0] if (user["role"] == "student" or student_id) and students else None
        activity = []
        security = []
        active_admins = active_scanners = active_hostels = 0
        if user["role"] != "student":
            admin_cond = "1=1" if user["role"] == "super-admin" else "hostel_scope=?"
            admin_args = [] if user["role"] == "super-admin" else [user.get("hostel_scope")]
            active_admins = db.execute(f"SELECT COUNT(*) n FROM users WHERE role='admin' AND active=1 AND {admin_cond}", admin_args).fetchone()["n"]
            active_scanners = db.execute(f"SELECT COUNT(*) n FROM scanner_devices d JOIN users u ON u.id=d.user_id WHERE d.last_seen>UTC_TIMESTAMP()-INTERVAL 60 SECOND AND u.active=1 AND u.scanner_enabled=1 AND {admin_cond}", admin_args).fetchone()["n"]
            active_hostels = db.execute("SELECT COUNT(*) n FROM hostels WHERE active=1" + (" AND name=?" if user["role"] == "admin" else ""), admin_args).fetchone()["n"]
            event_cond = "1=1" if user["role"] == "super-admin" else "e.user_id=?"
            event_args = [] if user["role"] == "super-admin" else [user["id"]]
            security = db.execute(f"SELECT e.id,e.outcome,e.created_at,u.name operator FROM scan_events e JOIN users u ON u.id=e.user_id WHERE {event_cond} ORDER BY e.id DESC LIMIT 12", event_args).fetchall()
            activity = db.execute("SELECT a.id,a.action,a.created_at,u.name actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_user_id WHERE a.actor_user_id=? AND a.entity_type IN ('student','user','hostel','session','settings','meal_attendance','profile_change_request','meal_menu','notification','backup','scanner','report','registration') ORDER BY a.id DESC LIMIT 6", (user["id"],)).fetchall()
    active_students = [s for s in students if s["active"] and s["approval_status"] == "APPROVED" and not s["deleted_at"]]
    active_ids={s["id"] for s in active_students}
    n = len(active_students)
    present = len({r["student_id"] for r in today_rows if r["student_id"] in active_ids})
    daily = []
    calendar = []
    total_expected = 0
    records_by_date = defaultdict(list)
    attendance_keys = set()
    for row in rows:
        records_by_date[row["meal_date"]].append(row)
        attendance_keys.add((row["student_id"],row["meal_date"],row["meal_type"]))
    session_map = {(s["session_date"], s["meal"], s["hostel"]): s for s in historical_sessions}
    window_cache = {}
    def status_for(day, item, student):
        key=(day,item["meal"],student["hostel"])
        if key not in window_cache:
            session = session_map.get(key) or session_map.get((day, item["meal"], "*"))
            if session is None:
                session = {"session_date": day, "start_time": item["start"], "end_time": item["end"], "timezone": config["timezone"], "active": item["enabled"]}
            window_cache[key]=(session["active"],*session_bounds(session))
        enabled,begin,finish=window_cache[key]
        if (student["id"],day,item["meal"]) in attendance_keys:
            return "Present"
        if not enabled:
            return "Disabled"
        if student["created_at"].replace(tzinfo=timezone.utc)>=finish:
            return "Not applicable"
        if now < begin:
            return "Session Not Started"
        return "Missed" if now >= finish else "Not Yet Marked"
    missed = 0
    today_status = []
    for i in range((end-start).days+1):
        day = start+timedelta(days=i)
        daily_rows = records_by_date[day]
        eligible = [s for s in students if s["created_at"].replace(tzinfo=timezone.utc).astimezone(now.tzinfo).date() <= day and (not s["deleted_at"] or s["deleted_at"].date() >= day)]
        expected = 0
        for student in eligible:
            for item in config["schedules"]:
                if meal and item["meal"] != meal:
                    continue
                state = status_for(day, item, student)
                if state in ("Present", "Missed", "Not Yet Marked"):
                    expected += 1
                if state == "Missed":
                    missed += 1
        total_expected += expected
        unique = len({r["student_id"] for r in daily_rows})
        daily.append({"day": str(day), "label": day.strftime("%d %b"), "present": unique, "absent": max(0, len(eligible)-unique), "meals": len(daily_rows), "expected": expected})
        calendar.append({"date": str(day), "meals": {item["meal"]: status_for(day, item, student_profile) for item in config["schedules"]} if student_profile else {}})
    if student_profile:
        for item in config["schedules"]:
            if item["enabled"]:
                today_status.append({"meal": item["meal"], "status": status_for(today, item, student_profile), "start": item["start"], "end": item["end"]})
    by_meal = Counter(r["meal_type"] for r in rows)
    hostels_count = Counter(r["hostel"] for r in rows)
    daily_hostel = defaultdict(set)
    for r in today_rows:
        if r["student_id"] not in active_ids:
            continue
        student = next((s for s in students if s["id"] == r["student_id"]), None)
        if student:
            daily_hostel[student["hostel"]].add(r["student_id"])
    hostel_stats = []
    for name in sorted({s["hostel"] for s in active_students}):
        count = sum(s["hostel"] == name for s in active_students)
        hostel_stats.append({"name": name, "students": count, "present": len(daily_hostel[name]), "attendance": round(len(daily_hostel[name])/count*100) if count else 0, "meals": hostels_count[name]})
    monthly = defaultdict(int)
    peaks = Counter()
    tz = now.tzinfo
    for r in rows:
        monthly[r["meal_date"].strftime("%Y-%m")] += 1
        peaks[r["scanned_at"].replace(tzinfo=timezone.utc).astimezone(tz).hour] += 1
    rate = round(len(rows)/total_expected*100, 1) if total_expected else 0
    summary = {"total_students": n, "approved_students": sum(s["approval_status"]=="APPROVED" and not s["deleted_at"] for s in students), "present_today": present, "absent_today": max(0,n-present), "attendance_rate": round(present/n*100,1) if n else 0,
               "total_meals": len(rows), "total_scans_today": len(today_rows), "missed_meals": missed, "period_attendance_rate": min(100,rate),
               "average_daily": round(sum(d["present"] for d in daily)/len(daily),1) if daily else 0, "active_hostels": active_hostels, "active_admins": active_admins, "active_scanners": active_scanners,
               "pending_attendance": sum(1 for x in today_status if x["status"] == "Not Yet Marked") if student_profile else max(0,n-present)}
    for meal_name in ("Breakfast", "Lunch", "Dinner", "Snacks"):
        summary[meal_name.lower()] = sum(r["meal_type"] == meal_name for r in today_rows)
    return {"summary": summary, "daily": daily, "weekly": daily[-7:], "monthly": [{"month": k, "value": v} for k,v in sorted(monthly.items())],
            "meals": [{"name": k, "value": by_meal[k]} for k in ("Breakfast", "Lunch", "Dinner", "Snacks")], "hostels": hostel_stats,
            "courses": [{"name": k or "Unspecified", "value": v} for k,v in Counter(s["course"] for s in active_students).items()],
            "years": [{"name": k or "Unspecified", "value": v} for k,v in Counter(r["academic_year"] for r in rows).items()],
            "peaks": [{"hour": f"{h:02}:00", "value": peaks[h]} for h in range(24)], "recent": recent, "student": student_profile,
            "calendar": calendar, "today_status": today_status, "sessions": sessions, "security": security, "activity": activity, "server_time": now.isoformat()}
