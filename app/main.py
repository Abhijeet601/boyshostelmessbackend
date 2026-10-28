from contextlib import asynccontextmanager
import logging

import pymysql
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .config import FRONTEND_ORIGINS
from .db import init_db
from .routers import auth, management, portal, reporting, registration


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="SmartMess — QR Attendance System", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.exception_handler(pymysql.IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": "This identifier already exists or the operation conflicts with retained records."})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Validation responses never echo passwords, image bytes or the submitted body.
    return JSONResponse(status_code=422, content={"detail": [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]})


@app.exception_handler(pymysql.MySQLError)
async def database_error(request, exc):
    logging.getLogger("smartmess").error("Database operation failed: %s code=%s", type(exc).__name__, exc.args[0] if exc.args else "unknown")
    return JSONResponse(status_code=503, content={"detail": "The database is temporarily unavailable. Please retry."})


@app.get("/api/health")
def health():
    return {"status": "ok", "application": "SmartMess"}


for module in (auth, management, portal, reporting, registration):
    app.include_router(module.router, prefix="/api")
