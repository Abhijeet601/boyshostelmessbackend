"""Browser QA only: temporary isolated database, removed on a clean shutdown."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
name = "smartmess_preview_" + uuid.uuid4().hex[:10]
os.environ["MYSQL_DATABASE"] = name
from app.db import get_db, init_db
from app.security import hash_password
from app.services import DEFAULT_SETTINGS, local_now
import json
import uvicorn

init_db()
with get_db() as db:
    db.execute("INSERT INTO hostels(name,description) VALUES ('North Residence','A connected community, one meal at a time.'),('South Residence','A welcoming home for every student.')")
    db.execute("INSERT INTO users(login_id,password_hash,role,name,email) VALUES (?,?,?,'Alex Morgan','alex@example.test')", ("QA-SUPER",hash_password("TestOnly-Safe!42"),"super-admin"))
    operator = db.execute("INSERT INTO users(login_id,password_hash,role,name,hostel_scope,email) VALUES (?,?,?,'Jordan Taylor','North Residence','jordan@example.test')", ("QA-ADMIN",hash_password("TestOnly-Safe!42"),"admin")).lastrowid
    config={**DEFAULT_SETTINGS,"academic_session":"2026–27"}
    config["schedules"]=[{**s,"enabled":s["meal"]=="Lunch","start":"00:00" if s["meal"]=="Lunch" else s["start"],"end":"23:59" if s["meal"]=="Lunch" else s["end"]} for s in config["schedules"]]
    db.execute("INSERT INTO app_settings(id,value_json) VALUES (1,?)", (json.dumps(config),))
    student_ids=[]
    for i,student_name in enumerate(["Jamie Chen","Riley Patel","Sam Wilson","Avery Kim","Taylor Singh","Casey Brown"]):
        sid=db.execute("INSERT INTO students(admission_number,registration_number,name,hostel,room_number,course,academic_year,created_at) VALUES (?,?,?,?,?,?,?,?)",(f"QA-ADM-{i+1}",f"QA-REG-{i+1}",student_name,"North Residence" if i<4 else "South Residence",str(101+i),"Computer Science" if i%2 else "Economics","1st Year" if i%2 else "2nd Year",datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(days=60))).lastrowid
        student_ids.append(sid)
        db.execute("INSERT INTO users(login_id,password_hash,role,name,student_id) VALUES (?,?,'student',?,?)", (f"QA-STUDENT-{i+1}",hash_password("TestOnly-Safe!42"),student_name,sid))
    import hashlib,secrets
    now=local_now(config)
    for i in range(1,25):
        day=now.date()-timedelta(days=i)
        for sid in student_ids:
            if (sid+i)%5==0:continue
            token=secrets.token_urlsafe(32)
            tid=db.execute("INSERT INTO qr_tokens(token_hash,student_id,user_id,expires_at,used_at) VALUES (?,?,1,UTC_TIMESTAMP(),UTC_TIMESTAMP())",(hashlib.sha256(token.encode()).hexdigest(),sid)).lastrowid
            db.execute("INSERT INTO meal_attendance(student_id,meal_date,meal_type,scanned_by,qr_token_id,scanned_at) VALUES (?,?,'Lunch',?,?,?)",(sid,day,operator,tid,datetime.combine(day,datetime.min.time())+timedelta(hours=7,minutes=15+sid)))
    for offset in range(7):
        day=now.date()-timedelta(days=now.weekday())+timedelta(days=offset)
        db.execute("INSERT INTO meal_menus(menu_date,breakfast,lunch,snacks,dinner) VALUES (?,?,?,?,?)",(day,"Fresh fruit, oats & tea","Rice, dal, seasonal vegetables","Fruit & refreshments","Roti, vegetables & salad"))
    db.execute("INSERT INTO notifications(title,body,kind) VALUES ('Welcome to your attendance workspace','Your live QR pass is ready. Head to the scanner for a quick check-in.','announcement')")
print(f"Isolated browser QA database: {name}",flush=True)
try:
    uvicorn.run("app.main:app",host="127.0.0.1",port=8012,log_level="warning")
finally:
    import pymysql
    from app import db as module
    assert name.startswith("smartmess_preview_") and len(name)==28
    connection=pymysql.connect(host=module.MYSQL_HOST,port=module.MYSQL_PORT,user=module.MYSQL_USER,password=module.MYSQL_PASSWORD)
    with connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE `{name}`")
    connection.close()
