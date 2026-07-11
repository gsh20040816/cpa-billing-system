from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .database import Database
from .services import BillingError, BillingService


ROOT = Path(os.getenv("BILLING_ASSET_ROOT", str(Path.cwd())))


class LoginLimiter:
    def __init__(self) -> None:
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, address: str) -> None:
        now = time.monotonic()
        bucket = self.attempts[address]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= 8:
            raise HTTPException(status_code=429, detail="too many login attempts")
        bucket.append(now)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    service = BillingService(settings, database)
    service.bootstrap()
    app = FastAPI(title="CPA Billing", docs_url=None, redoc_url=None)
    app.state.service = service
    templates = Jinja2Templates(directory=ROOT / "templates")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    limiter = LoginLimiter()

    def current(request: Request) -> tuple[Any, Any]:
        auth = service.get_session(request.cookies.get("billing_session"))
        if auth is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return auth

    def admin(auth: tuple[Any, Any] = Depends(current)) -> tuple[Any, Any]:
        if not auth[1].is_admin:
            raise HTTPException(status_code=403)
        return auth

    def csrf(request: Request, auth: tuple[Any, Any]) -> None:
        token = request.headers.get("x-csrf-token") or ""
        if request.method == "POST" and not token:
            raise HTTPException(status_code=403, detail="missing CSRF token")
        if token and token != auth[0].csrf_token:
            raise HTTPException(status_code=403, detail="invalid CSRF token")

    @app.exception_handler(BillingError)
    async def billing_error(_: Request, exc: BillingError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; script-src 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Any:
        if service.get_session(request.cookies.get("billing_session")):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {})

    @app.post("/auth/api-key/login")
    async def login(request: Request, api_key: str = Form(...)) -> Any:
        limiter.check(request.client.host if request.client else "unknown")
        authenticated = await asyncio.to_thread(service.authenticate_key, api_key)
        if authenticated is None:
            await asyncio.sleep(0.35)
            return templates.TemplateResponse(request, "login.html", {"error": "API Key 无效、已撤销或尚未通过 Telegram 注册。"}, status_code=401)
        user, key = authenticated
        token, _ = service.create_session(user.telegram_user_id, key.id)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("billing_session", token, secure=True, httponly=True, samesite="lax", max_age=settings.session_ttl_seconds)
        return response

    @app.post("/auth/logout")
    def logout(request: Request, auth: tuple[Any, Any] = Depends(current)) -> Any:
        csrf(request, auth)
        token = request.cookies.get("billing_session")
        if token:
            service.revoke_session(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("billing_session")
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, auth: tuple[Any, Any] = Depends(current)) -> Any:
        return templates.TemplateResponse(request, "dashboard.html", {"auth": auth, "data": service.dashboard()})

    @app.get("/users/{user_id}", response_class=HTMLResponse)
    def user_page(user_id: int, request: Request, auth: tuple[Any, Any] = Depends(current)) -> Any:
        include_keys = auth[1].is_admin or auth[1].telegram_user_id == user_id
        return templates.TemplateResponse(request, "user.html", {"auth": auth, "data": service.user_summary(user_id, include_keys=include_keys), "include_keys": include_keys})

    @app.get("/me", response_class=HTMLResponse)
    def me(request: Request, auth: tuple[Any, Any] = Depends(current)) -> Any:
        return templates.TemplateResponse(request, "me.html", {"auth": auth, "data": service.user_summary(auth[1].telegram_user_id, include_keys=True)})

    @app.post("/me/key-actions")
    async def key_action(request: Request, action: str = Form(...), current_api_key: str = Form(...), target_key_id: int | None = Form(None),
                         auth: tuple[Any, Any] = Depends(current)) -> Any:
        csrf(request, auth)
        token = await asyncio.to_thread(service.request_key_action, auth[1].telegram_user_id, current_api_key, action, target_key_id)
        if settings.telegram_token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                        json={"chat_id": auth[1].telegram_user_id, "text": f"Web 发起了 API Key {action} 操作。确认请执行：\n/confirm {token}"},
                    )
            except httpx.HTTPError:
                pass
        return templates.TemplateResponse(request, "action_pending.html", {"auth": auth, "token": token, "action": action})

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, auth: tuple[Any, Any] = Depends(admin)) -> Any:
        return templates.TemplateResponse(request, "admin.html", {"auth": auth, "reconciliation": service.reconciliation(),
                                                                   "usage": service.usage_summary(), "admin": service.admin_snapshot()})

    @app.post("/admin/cycles")
    def create_cycle(request: Request, name: str = Form(...), start: str = Form(...), end: str = Form(...),
                     fixed_cost: str = Form(...), waiver: str = Form(""), auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.create_cycle(name, start, end, int(round(float(fixed_cost) * 100)), waiver.strip() or None)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/cycles/{cycle_name}/preview")
    def preview(cycle_name: str, request: Request, auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.preview_cycle(cycle_name)
        return RedirectResponse("/", status_code=303)

    @app.post("/admin/cycles/{cycle_name}/close")
    def close(cycle_name: str, request: Request, confirm_waiver: bool = Form(False), auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.close_cycle(cycle_name, auth[1].telegram_user_id, confirm_waiver)
        return RedirectResponse("/", status_code=303)

    @app.post("/admin/adjustments")
    def adjustment(request: Request, cycle: str = Form(...), telegram_user_id: int = Form(...), amount_cents: int = Form(...),
                   reason: str = Form(...), auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.add_adjustment(cycle, telegram_user_id, amount_cents, reason, auth[1].telegram_user_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/ownership-transfers")
    def ownership_transfer(request: Request, key_id: int = Form(...), telegram_user_id: int = Form(...), reason: str = Form(...),
                           auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.transfer_key(key_id, telegram_user_id, auth[1].telegram_user_id, reason)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/pools")
    def pool(request: Request, name: str = Form(...), auth_pattern: str = Form(""), model_pattern: str = Form(""),
             priority: int = Form(100), auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.create_pool(name, auth_pattern, model_pattern, priority)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/pricing-versions/import")
    def import_pricing(request: Request, name: str = Form(...), auth: tuple[Any, Any] = Depends(admin)) -> Any:
        csrf(request, auth)
        service.import_cpamp_prices(name)
        return RedirectResponse("/admin", status_code=303)

    @app.get("/api/session")
    def api_session(auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return {"telegram_user_id": auth[1].telegram_user_id, "is_admin": auth[1].is_admin, "csrf_token": auth[0].csrf_token}

    @app.get("/api/dashboard")
    def api_dashboard(_: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.dashboard()

    @app.get("/api/users/{user_id}/summary")
    def api_user(user_id: int, auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.user_summary(user_id, include_keys=auth[1].is_admin or auth[1].telegram_user_id == user_id)

    @app.get("/api/me/keys")
    def api_keys(auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.user_summary(auth[1].telegram_user_id, include_keys=True)

    return app
