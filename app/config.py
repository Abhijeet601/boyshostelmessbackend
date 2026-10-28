import os
from pathlib import Path
from urllib.parse import unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parents[1]


def _load_env_file() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

def resolve_mysql_settings(environment):
    """Accept Railway's connection URL or individual connection fields."""
    connection_url = environment.get("MYSQL_URL") or environment.get("DATABASE_URL") or environment.get("MYSQL_PUBLIC_URL")
    url_fields = {}
    if connection_url:
        try:
            parsed = urlsplit(connection_url)
            if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname:
                raise ValueError()
            url_fields = {"host":parsed.hostname,"port":parsed.port or 3306,"user":unquote(parsed.username or "root"),"password":unquote(parsed.password or ""),"database":unquote(parsed.path.lstrip("/")) or "mess"}
        except ValueError:
            # Connection URLs contain credentials: keep them out of errors.
            raise ValueError("Invalid MySQL connection URL. Use mysql://user:password@host:port/database.") from None
    result = {}
    for key, default in (("host", "127.0.0.1"), ("port", 3306), ("user", "root"), ("password", ""), ("database", "mess")):
        result[key] = environment.get("MYSQL_"+key.upper()) or environment.get("MYSQL"+key.upper()) or url_fields.get(key,default)
    try:
        result["port"] = int(result["port"])
        if not 1 <= result["port"] <= 65535:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("MySQL port must be a number between 1 and 65535.") from None
    return result


_mysql = resolve_mysql_settings(os.environ)
MYSQL_HOST = _mysql["host"]
MYSQL_PORT = _mysql["port"]
MYSQL_USER = _mysql["user"]
MYSQL_PASSWORD = _mysql["password"]
MYSQL_DATABASE = _mysql["database"]
APP_SECRET = os.getenv("APP_SECRET", "dev-secret-change-before-production")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "28800"))
FRONTEND_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174",
    ).split(",")
    if origin.strip()
]
