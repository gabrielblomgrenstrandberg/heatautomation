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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config
from ..house.config import HouseConfig
from .house_config import ConfigurationConflict, apply_configuration, read_configuration, save_configuration
from .status import StatusUnavailable, read_status
from .rooms import read_history, read_house, read_plan, read_schedules, set_override, set_room_settings, set_schedules
from .discovery import read_discovery, request_scan


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


class RoomSettings(BaseModel):
    target_c: float = Field(ge=-30, le=60, allow_inf_nan=False)
    min_c: float = Field(ge=-30, le=60, allow_inf_nan=False)
    max_c: float = Field(ge=-30, le=60, allow_inf_nan=False)
    max_preheat_c: Optional[float] = Field(default=None, ge=0, le=2, allow_inf_nan=False)


class RoomOverride(BaseModel):
    target_c: float = Field(ge=-30, le=60, allow_inf_nan=False)
    minutes: int = Field(ge=1, le=10080, strict=True)


class HouseConfigurationUpdate(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    config: HouseConfig


class HouseConfigurationApply(BaseModel):
    revision: str = Field(min_length=64, max_length=64)


class RoomScheduleEntry(BaseModel):
    weekday: int = Field(ge=0, le=6, strict=True)
    start_min: int = Field(ge=0, le=1439, strict=True)
    end_min: int = Field(ge=1, le=1440, strict=True)
    target_c: float = Field(ge=-30, le=60, allow_inf_nan=False)


class RoomScheduleUpdate(BaseModel):
    entries: list[RoomScheduleEntry] = Field(max_length=672)


def create_app(password=None, database_path=None, origin=None, secure_cookie=None):
    password = password if password is not None else os.environ.get("HEATAUTOMATION_WEB_PASSWORD", "")
    if len(password) < 32:
        raise RuntimeError("HEATAUTOMATION_WEB_PASSWORD must be at least 32 characters.")
    origin = origin or os.environ.get("HEATAUTOMATION_WEB_ORIGIN", "http://127.0.0.1:8000")
    if secure_cookie is None:
        secure_cookie = os.environ.get("HEATAUTOMATION_WEB_SECURE_COOKIE", "").lower() == "true"
    database_path = database_path or config.THERMAL_DATABASE_PATH
    house_config_path = Path(os.environ.get("HEATAUTOMATION_HOUSE_CONFIG", "config/house.json")).resolve()
    sessions = {}
    attempts = []
    app = FastAPI(title="Heat Automation monitoring API")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": [
            {"type": item["type"], "loc": list(item["loc"]), "msg": item["msg"]}
            for item in error.errors()
        ]})

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

    @app.get("/api/integrations")
    async def integrations(request: Request):
        require_session(request)
        enabled = {
            "sensibo": config.SENSIBO_ENABLED,
            "kmp": config.KMP_ENABLED,
            "tibber": config.TIBBER_ENABLED,
            "weather": config.WEATHER_ENABLED,
        }
        automatic_switching_available = all(enabled.values())
        enabled["tado"] = config.TADO_ENABLED
        return {"enabled": enabled, "automatic_switching_available": automatic_switching_available}

    @app.get("/api/house")
    async def house(request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_house, database_path)
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/discovery")
    async def discovery(request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_discovery, database_path, house_config_path)
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/discovery", status_code=202)
    async def discover(request: Request):
        require_session(request)
        require_origin(request)
        try:
            await run_in_threadpool(request_scan, database_path, house_config_path)
            return {"message": "Scan requested; the controller will process it subject to rate limits."}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/house-configuration")
    async def house_configuration(request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_configuration, house_config_path)
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.put("/api/house-configuration")
    async def update_house_configuration(payload: HouseConfigurationUpdate, request: Request):
        require_session(request)
        require_origin(request)
        try:
            revision = await run_in_threadpool(
                save_configuration, house_config_path, payload.revision, payload.config)
            return {"revision": revision}
        except ConfigurationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/house-configuration/apply", status_code=204)
    async def apply_house_configuration(payload: HouseConfigurationApply, request: Request):
        require_session(request)
        require_origin(request)
        try:
            await run_in_threadpool(apply_configuration, house_config_path, database_path, payload.revision)
        except ConfigurationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/rooms/{room_id}/history")
    async def room_history(room_id: str, request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_history, database_path, room_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/rooms/{room_id}/schedules")
    async def room_schedules(room_id: str, request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_schedules, database_path, room_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.put("/api/rooms/{room_id}/schedules", status_code=204)
    async def update_room_schedules(room_id: str, payload: RoomScheduleUpdate, request: Request):
        require_session(request)
        require_origin(request)
        try:
            await run_in_threadpool(set_schedules, database_path, room_id,
                                    [entry.model_dump() for entry in payload.entries])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/room-plan")
    async def room_plan(request: Request):
        require_session(request)
        try:
            return await run_in_threadpool(read_plan, database_path)
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.put("/api/rooms/{room_id}/settings", status_code=204)
    async def room_settings(room_id: str, payload: RoomSettings, request: Request):
        require_session(request)
        require_origin(request)
        try:
            await run_in_threadpool(set_room_settings, database_path, room_id,
                                    payload.target_c, payload.min_c, payload.max_c,
                                    payload.max_preheat_c)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except StatusUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/rooms/{room_id}/override", status_code=204)
    async def room_override(room_id: str, payload: RoomOverride, request: Request):
        require_session(request)
        require_origin(request)
        try:
            await run_in_threadpool(set_override, database_path, room_id,
                                    payload.target_c, payload.minutes)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
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
                try:
                    house_data = await run_in_threadpool(read_house, database_path)
                    await websocket.send_json({"type": "house", "data": jsonable_encoder(house_data)})
                except StatusUnavailable as error:
                    await websocket.send_json({"type": "house_error", "message": str(error)})
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
