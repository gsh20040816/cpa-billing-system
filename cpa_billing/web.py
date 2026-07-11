from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .database import Database
from .security import constant_equal
from .services import BillingError, BillingService


ROOT = Path(os.getenv("BILLING_ASSET_ROOT", str(Path.cwd())))
USER_COOKIE = "__Host-billing_session"
ADMIN_COOKIE = "__Host-billing_admin_session"
LOGGER = logging.getLogger(__name__)


class LoginLimiter:
    def __init__(self, maximum: int = 8, window_seconds: int = 60) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, current: float) -> deque[float]:
        bucket = self.attempts[key]
        while bucket and current - bucket[0] > self.window_seconds:
            bucket.popleft()
        if not bucket:
            self.attempts.pop(key, None)
            return deque()
        return bucket

    def check(self, key: str) -> None:
        if len(self._prune(key, time.monotonic())) >= self.maximum:
            raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试。",
                                headers={"Retry-After": str(self.window_seconds)})

    def failure(self, key: str) -> None:
        current = time.monotonic()
        bucket = self._prune(key, current)
        if key not in self.attempts:
            self.attempts[key] = bucket
        bucket.append(current)
        if len(self.attempts) > 4096:
            for candidate in list(self.attempts):
                self._prune(candidate, current)

    def success(self, key: str) -> None:
        self.attempts.pop(key, None)


def _client_address(request: Request) -> str:
    forwarded = (request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip")
                 or request.headers.get("x-forwarded-for", "").split(",", 1)[0]).strip()
    address = forwarded or (request.client.host if request.client else "unknown")
    return address[:128]


def _money_to_cents(value: str) -> int:
    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise BillingError("固定成本格式无效") from exc
    if not amount.is_finite() or amount < 0:
        raise BillingError("固定成本必须是非负金额")
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount != quantized:
        raise BillingError("固定成本最多保留两位小数")
    return int(quantized * 100)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    service = BillingService(settings, database)
    service.bootstrap()
    app = FastAPI(title="CPA Billing", docs_url=None, redoc_url=None)
    app.state.service = service
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.undefined = StrictUndefined
    templates.env.filters["number"] = lambda value: f"{int(value or 0):,}"
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    limiter = LoginLimiter()

    def render(request: Request, name: str, context: dict[str, Any] | None = None,
               status_code: int = 200) -> Any:
        values: dict[str, Any] = {"auth": None, "admin_auth": None, "error": None}
        if context:
            values.update(context)
        return templates.TemplateResponse(request, name, values, status_code=status_code)

    def current(request: Request) -> tuple[Any, Any]:
        auth = service.get_session(request.cookies.get(USER_COOKIE))
        if auth is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return auth

    def page_current(request: Request) -> tuple[Any, Any] | None:
        return service.get_session(request.cookies.get(USER_COOKIE))

    def admin_current(request: Request) -> Any:
        auth = service.get_admin_session(request.cookies.get(ADMIN_COOKIE))
        if auth is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return auth

    def page_admin_current(request: Request) -> Any | None:
        return service.get_admin_session(request.cookies.get(ADMIN_COOKIE))

    def verify_csrf(session: Any, token: str) -> None:
        if not token or not constant_equal(token, session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试。")

    def page_error_message(status_code: int, detail: Any) -> str:
        if status_code == 401:
            return "登录已失效，请重新登录。"
        if status_code == 403:
            return str(detail) if detail and detail != "Forbidden" else "没有权限执行此操作。"
        if status_code == 404:
            return "请求的页面不存在。"
        if status_code == 429:
            return str(detail)
        return str(detail) if detail else "请求处理失败。"

    @app.exception_handler(BillingError)
    async def billing_error(request: Request, exc: BillingError) -> Any:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=400)
        return render(request, "error.html", {"status_code": 400, "message": str(exc)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Any:
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return render(request, "error.html", {
            "status_code": exc.status_code,
            "message": page_error_message(exc.status_code, exc.detail),
        }, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Any:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return render(request, "error.html", {
            "status_code": 422,
            "message": "表单字段不完整或格式无效，请返回检查后重试。",
        }, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; style-src 'self'; img-src 'self' data:; script-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(ROOT / "static" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Any:
        if page_current(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html")

    @app.post("/auth/api-key/login")
    async def login(request: Request, api_key: str = Form(...)) -> Any:
        limiter_key = f"user:{_client_address(request)}"
        limiter.check(limiter_key)
        try:
            authenticated = await asyncio.to_thread(service.authenticate_key, api_key)
        except (httpx.HTTPError, BillingError) as exc:
            LOGGER.warning("API Key authentication dependency failed: %s", type(exc).__name__)
            return render(request, "login.html", {"error": "认证服务暂时不可用，请稍后再试。"}, status_code=503)
        if authenticated is None:
            limiter.failure(limiter_key)
            await asyncio.sleep(0.35)
            return render(request, "login.html", {"error": "API Key 无效、已撤销或尚未通过 Telegram 注册。"}, status_code=401)
        limiter.success(limiter_key)
        user, key = authenticated
        token, _ = service.create_session(user.telegram_user_id, key.id)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(USER_COOKIE, token, secure=True, httponly=True, samesite="lax", path="/",
                            max_age=settings.session_ttl_seconds)
        response.delete_cookie("billing_session", path="/")
        return response

    @app.post("/auth/logout")
    def logout(request: Request, csrf_token: str = Form("", alias="_csrf"),
               auth: tuple[Any, Any] = Depends(current)) -> Any:
        verify_csrf(auth[0], csrf_token)
        token = request.cookies.get(USER_COOKIE)
        if token:
            service.revoke_session(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(USER_COOKIE, path="/")
        response.delete_cookie("billing_session", path="/")
        return response

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login_page(request: Request) -> Any:
        if page_admin_current(request):
            return RedirectResponse("/admin", status_code=303)
        return render(request, "admin_login.html")

    @app.post("/auth/admin/login")
    async def admin_login(request: Request, management_token: str = Form(...)) -> Any:
        limiter_key = f"admin:{_client_address(request)}"
        limiter.check(limiter_key)
        if not service.authenticate_admin_token(management_token):
            limiter.failure(limiter_key)
            await asyncio.sleep(0.5)
            return render(request, "admin_login.html", {"error": "管理 token 无效。"}, status_code=401)
        limiter.success(limiter_key)
        token, _ = service.create_admin_session()
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(ADMIN_COOKIE, token, secure=True, httponly=True, samesite="strict", path="/",
                            max_age=settings.session_ttl_seconds)
        return response

    @app.post("/auth/admin/logout")
    def admin_logout(request: Request, csrf_token: str = Form("", alias="_csrf"),
                     auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        token = request.cookies.get(ADMIN_COOKIE)
        if token:
            service.revoke_admin_session(token)
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, cycle: str | None = Query(None)) -> Any:
        auth = page_current(request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        return render(request, "dashboard.html", {"auth": auth, "data": service.dashboard(cycle)})

    @app.get("/users/{user_id}", response_class=HTMLResponse)
    def user_page(user_id: int, request: Request, cycle: str | None = Query(None)) -> Any:
        auth = page_current(request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        include_keys = auth[1].telegram_user_id == user_id
        return render(request, "user.html", {"auth": auth,
                      "data": service.user_summary(user_id, cycle, include_keys=include_keys),
                      "include_keys": include_keys})

    @app.get("/me", response_class=HTMLResponse)
    def me(request: Request, cycle: str | None = Query(None)) -> Any:
        auth = page_current(request)
        if auth is None:
            return RedirectResponse("/login", status_code=303)
        return render(request, "me.html", {"auth": auth,
                      "data": service.user_summary(auth[1].telegram_user_id, cycle, include_keys=True),
                      "include_keys": True})

    @app.post("/me/key-actions")
    async def key_action(request: Request, action: str = Form(...), current_api_key: str = Form(...),
                         target_key_id: int | None = Form(None), csrf_token: str = Form("", alias="_csrf"),
                         auth: tuple[Any, Any] = Depends(current)) -> Any:
        verify_csrf(auth[0], csrf_token)
        token = await asyncio.to_thread(service.request_key_action, auth[1].telegram_user_id,
                                        current_api_key, action, target_key_id)
        notification_sent = False
        if settings.telegram_token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                        json={"chat_id": auth[1].telegram_user_id,
                              "text": f"Web 发起了 API Key {action} 操作。确认请执行：\n/confirm {token}"},
                    )
                    response.raise_for_status()
                    notification_sent = True
            except httpx.HTTPStatusError as exc:
                LOGGER.warning("Telegram confirmation notification failed with status %s", exc.response.status_code)
            except httpx.HTTPError as exc:
                LOGGER.warning("Telegram confirmation notification failed: %s", type(exc).__name__)
        return render(request, "action_pending.html", {"auth": auth, "token": token,
                      "action": action, "notification_sent": notification_sent})

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request) -> Any:
        auth = page_admin_current(request)
        if auth is None:
            return RedirectResponse("/admin/login", status_code=303)
        return render(request, "admin.html", {"admin_auth": auth, "reconciliation": service.reconciliation(),
                      "usage": service.usage_summary(), "accounts": service.account_usage(),
                      "admin": service.admin_snapshot()})

    @app.post("/admin/cycles")
    def create_cycle(request: Request, name: str = Form(...), start: str = Form(...), end: str = Form(...),
                     fixed_cost: str = Form(...), waiver: str = Form(""),
                     csrf_token: str = Form("", alias="_csrf"), auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        service.create_cycle(name, start, end, _money_to_cents(fixed_cost), waiver.strip() or None,
                             operator_type="web-admin", operator_id="admin-token")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/cycles/{cycle_name}/preview")
    def preview(cycle_name: str, csrf_token: str = Form("", alias="_csrf"),
                auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        service.preview_cycle(cycle_name)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/cycles/{cycle_name}/close")
    def close(cycle_name: str, confirm_close: bool = Form(False), confirm_waiver: bool = Form(False),
              csrf_token: str = Form("", alias="_csrf"), auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        if not confirm_close:
            raise BillingError("关闭周期前必须确认账单将被冻结")
        service.close_cycle(cycle_name, None, confirm_waiver, operator_type="web-admin")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/adjustments")
    def adjustment(cycle: str = Form(...), telegram_user_id: int = Form(...), amount_cents: int = Form(...),
                   reason: str = Form(...), csrf_token: str = Form("", alias="_csrf"),
                   auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        service.add_adjustment(cycle, telegram_user_id, amount_cents, reason, None, operator_type="web-admin")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/ownership-transfers")
    def ownership_transfer(key_id: int = Form(...), telegram_user_id: int = Form(...), reason: str = Form(...),
                           confirm_transfer: bool = Form(False), csrf_token: str = Form("", alias="_csrf"),
                           auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        if not confirm_transfer:
            raise BillingError("变更 Key 归属前必须明确确认")
        service.transfer_key(key_id, telegram_user_id, None, reason, operator_type="web-admin")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/pools")
    def pool(name: str = Form(...), auth_pattern: str = Form(""), model_pattern: str = Form(""),
             priority: int = Form(100), csrf_token: str = Form("", alias="_csrf"),
             auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        service.create_pool(name, auth_pattern, model_pattern, priority,
                            operator_type="web-admin", operator_id="admin-token")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/pricing-versions/import")
    def import_pricing(name: str = Form(...), confirm_import: bool = Form(False),
                       csrf_token: str = Form("", alias="_csrf"),
                       auth: Any = Depends(admin_current)) -> Any:
        verify_csrf(auth, csrf_token)
        if not confirm_import:
            raise BillingError("导入价格版本前必须明确确认")
        service.import_cpamp_prices(name, operator_type="web-admin", operator_id="admin-token", allow_existing=False)
        return RedirectResponse("/admin", status_code=303)

    @app.get("/api/session")
    def api_session(auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return {"telegram_user_id": auth[1].telegram_user_id, "is_admin": False,
                "csrf_token": auth[0].csrf_token}

    @app.get("/api/admin/session")
    def api_admin_session(auth: Any = Depends(admin_current)) -> dict[str, Any]:
        return {"is_admin": True, "csrf_token": auth.csrf_token}

    @app.get("/api/dashboard")
    def api_dashboard(cycle: str | None = Query(None), _: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.dashboard(cycle)

    @app.get("/api/users/{user_id}/summary")
    def api_user(user_id: int, cycle: str | None = Query(None),
                 auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.user_summary(user_id, cycle, include_keys=auth[1].telegram_user_id == user_id)

    @app.get("/api/me/keys")
    def api_keys(cycle: str | None = Query(None), auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.user_summary(auth[1].telegram_user_id, cycle, include_keys=True)

    return app
