"""FastAPI application factory; this process never starts the controller."""

import asyncio
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config
from .status import StatusUnavailable, read_status


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class Device(BaseModel):
    on: Optional[bool] = None
    mode: Optional[str] = None
    capacity_kw: Optional[float] = None


class Status(BaseModel):
    state: Literal["current", "stale", "unavailable"]
    message: str
    observed_at: Optional[str] = None
    indoor_c: Optional[float] = None
    outdoor_c: Optional[float] = None
    target_c: Optional[float] = None
    price_sek_kwh: Optional[float] = None
    heatpump: Optional[Device] = None
    pelletstove: Optional[Device] = None
    estimated_cop: Optional[float] = None
    estimated_demand_kw: Optional[float] = None
    estimated_pump_cost_sek_kwh: Optional[float] = None


def create_app(password=None, database_path=None, origin=None, secure_cookie=None):
    password = password if password is not None else os.environ.get("HEATAUTOMATION_WEB_PASSWORD", "")
    if len(password) < 32:
        raise RuntimeError("HEATAUTOMATION_WEB_PASSWORD must be at least 32 characters.")
    origin = origin or os.environ.get("HEATAUTOMATION_WEB_ORIGIN", "http://127.0.0.1:8000")
    if secure_cookie is None:
        secure_cookie = os.environ.get("HEATAUTOMATION_WEB_SECURE_COOKIE", "").lower() == "true"
    database_path = database_path or config.THERMAL_DATABASE_PATH
    sessions = {}
    attempts = []
    app = FastAPI(title="Heat Automation monitoring API")

    @app.middleware("http")
    async def prevent_api_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(token):
        expiry = sessions.get(token)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            del sessions[token]
            return False
        return True

    def require_session(request):
        if not authenticated(request.cookies.get("heat_session")):
            raise HTTPException(status_code=401, detail="Authentication required")

    def require_origin(request):
        if request.headers.get("origin") != origin:
            raise HTTPException(status_code=403, detail="Untrusted origin")

    @app.post("/api/session", status_code=204)
    async def login(payload: Login, request: Request, response: Response):
        require_origin(request)
        now = time.monotonic()
        attempts[:] = [attempt for attempt in attempts if now - attempt < 60]
        if len(attempts) >= 5:
            raise HTTPException(status_code=429, detail="Too many login attempts")
        attempts.append(now)
        if not hmac.compare_digest(payload.password.encode("utf-8"), password.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        attempts.clear()
        sessions_copy = list(sessions.items())
        for token, expiry in sessions_copy:
            if expiry <= now:
                del sessions[token]
        token = secrets.token_urlsafe(32)
        sessions[token] = now + 8 * 3600
        response.set_cookie("heat_session", token, httponly=True, secure=secure_cookie,
                            samesite="strict", max_age=8 * 3600, path="/")

    @app.delete("/api/session", status_code=204)
    async def logout(request: Request, response: Response):
        require_origin(request)
        sessions.pop(request.cookies.get("heat_session"), None)
        response.delete_cookie("heat_session", path="/")

    @app.get("/api/status", response_model=Status, responses={
        401: {"description": "Authentication required"},
        503: {"description": "Observation database unavailable"},
    })
    async def status(request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_status, database_path)
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.websocket("/api/live")
    async def live(websocket: WebSocket):
        token = websocket.cookies.get("heat_session")
        if websocket.headers.get("origin") != origin or not authenticated(token):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while authenticated(token):
                try:
                    snapshot = await run_in_threadpool(read_status, database_path)
                    await websocket.send_json({
                        "type": "status", "data": jsonable_encoder(Status(**snapshot))
                    })
                except StatusUnavailable as error:
                    await websocket.send_json({"type": "error", "message": str(error)})
                await asyncio.sleep(5)
            await websocket.close(code=1008)
        except WebSocketDisconnect:
            pass

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")
    if (dist / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(str(dist / "index.html"))
    return app
