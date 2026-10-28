# boyshostelmessbackend

SmartMess API: a FastAPI and MySQL 8 backend for secure hostel meal attendance, student registration and approval, QR scanning, reports, and administration. Requires Python 3.11 or newer and MySQL 8.

## Run locally

Install the dependencies from this repository's root:

```sh
python -m pip install -r requirements.txt
```

Create a local `.env` using `.env.example`. Set `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, a private `APP_SECRET`, and the comma-separated `FRONTEND_ORIGINS` allowed to call the API. Railway's `MYSQLHOST`, `MYSQLPORT`, `MYSQLUSER`, `MYSQLPASSWORD` and `MYSQLDATABASE` variables are also supported.

```sh
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health check: `/api/health`. Interactive API documentation: `/docs`. Startup creates the configured database when necessary and applies the included SQL migrations. Before upgrading an existing database, automatic full backups are saved locally in `backups/`. Existing account passwords, students, hostels and attendance are preserved.

To provision the first Super Admin with your own credentials, use `python -m app.seed_admin --help`. No demo users are created automatically. Create hostels and assign administrators before reviewing student applications.

## Authentication and attendance

Authentication uses existing PBKDF2 password hashes and expiring signed bearer tokens. The server revalidates active accounts and roles on each authenticated request. Every new login has a unique session identity. Logout revokes only the presented token; password and administrator changes invalidate all of that account's sessions. Admin access is limited to the assigned hostel. The login request's optional legacy `portal` field never grants privileges.

QR codes contain cryptographically random opaque tokens, stored as SHA-256 hashes. Server time, expiry, account/QR status, hostel permissions, and active attendance windows are validated in one transaction. Student and token row locks plus database uniqueness constraints prevent replay and concurrent duplicate attendance.

The API exposes authentication/profile, students, administrators, hostels, attendance, CSV/XLSX exports, overview analytics, recurring configuration, dated sessions, scanner validation/health, QR policy activity, meal menus, profile approvals, notifications, audit logs, and backups. Interactive API documentation is at `/docs`.

Student enrollment adds public registration, restricted status tracking, private sanitized photo uploads, hostel-scoped application review, atomic approval and reasoned suspension/reactivation. Pending and rejected applicants cannot access student dashboards or generate attendance QR tokens. Existing students migrate as approved after a full pre-registration backup. Pillow validates and sanitizes uploaded photos before private database storage.

## Deployment and verification

`railway.json` and `Procfile` start Uvicorn using the platform's `PORT` variable. Configure the database variables, `APP_SECRET`, and your deployed frontend's exact origin through the hosting platform. The [frontend repository](https://github.com/Abhijeet601/boyshostelmessfrontend) uses `VITE_API_BASE_URL` to select this API when hosted separately.

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p test_attendance.py -v
python -m compileall -q app
```

Integration tests create and remove their own uniquely named MySQL database; the test database user needs permission to create and drop those temporary databases. Browser test scripts use the isolated fixture server in `tests/serve_preview.py` and a frontend on port 5175, with its API proxy targeting port 8012. Test users and passwords are isolated fixtures, not production accounts.

Set a private `APP_SECRET` in production; changing it invalidates existing tokens. Serve the API over HTTPS, configure allowed frontend origins, and give the application database user only its necessary privileges. The current bootstrap creates the configured database when needed. Database-backed fixed-window limits protect login, QR issuance, scanning, and password changes.

Do not publish `.env`, local backups, or QA fixtures as production data. Backup restore adds missing records only; it does not roll existing data back. Historical account passwords are preserved during migration.
