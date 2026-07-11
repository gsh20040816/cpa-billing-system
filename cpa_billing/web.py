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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .database import Database
from .domain import NANO_USD
from .security import constant_equal
from .services import BillingDependencyError, BillingError, BillingService


ROOT = Path(os.getenv("BILLING_ASSET_ROOT", str(Path.cwd())))
FRONTEND_DIST = Path(os.getenv("BILLING_FRONTEND_DIST", str(ROOT / "frontend" / "dist")))
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
            raise HTTPException(
                status_code=429,
                detail="登录失败次数过多，请稍后再试。",
                headers={"Retry-After": str(self.window_seconds)},
            )

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


class CooldownLimiter:
    def __init__(self, cooldown_seconds: int) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.next_allowed: dict[str, float] = {}

    def check_and_mark(self, key: str) -> None:
        current = time.monotonic()
        retry_at = self.next_allowed.get(key, 0)
        if current < retry_at:
            retry_after = max(1, round(retry_at - current))
            raise HTTPException(
                status_code=429,
                detail="额度刷新操作过于频繁，请稍后再试。",
                headers={"Retry-After": str(retry_after)},
            )
        self.next_allowed[key] = current + self.cooldown_seconds
        if len(self.next_allowed) > 4096:
            self.next_allowed = {item: value for item, value in self.next_allowed.items() if value > current}


class UserLoginPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class AdminLoginPayload(BaseModel):
    management_token: str = Field(min_length=1, max_length=512)


class KeyActionPayload(BaseModel):
    action: str
    current_api_key: str = Field(min_length=1, max_length=512)
    target_key_id: int | None = Field(default=None, ge=1)


class KeyNamePayload(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class AccountRefreshPayload(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=100)


class PoolCostPayload(BaseModel):
    pool_id: int = Field(ge=1)
    fixed_cost: str


class CyclePayload(BaseModel):
    name: str
    start: str
    end: str
    fixed_cost: str = "0"
    gradient_rule_id: int | None = Field(default=None, ge=1)
    pool_costs: list[PoolCostPayload] = Field(default_factory=list)
    waiver: str | None = Field(default=None, max_length=1000)


class CloseCyclePayload(BaseModel):
    confirm_close: bool = False
    confirm_waiver: bool = False


class AdjustmentPayload(BaseModel):
    cycle: str
    telegram_user_id: int
    amount_cents: int
    reason: str = Field(min_length=1, max_length=1000)


class ManualUsageAdjustmentPayload(BaseModel):
    cycle: str = Field(min_length=1, max_length=80)
    pool_id: int = Field(ge=1)
    telegram_user_id: int
    amount_usd: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


class OwnershipTransferPayload(BaseModel):
    key_id: int = Field(ge=1)
    telegram_user_id: int
    reason: str = Field(min_length=1, max_length=1000)
    confirm_transfer: bool = False


class PoolPayload(BaseModel):
    name: str
    auth_pattern: str | None = None
    model_pattern: str | None = None
    priority: int = 100


class PricingImportPayload(BaseModel):
    name: str
    confirm_import: bool = False


class PricingSyncPayload(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


class GradientRulePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    tiers: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class ReasonPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class CycleConfigurationPayload(BaseModel):
    gradient_rule_id: int = Field(ge=1)
    pool_costs: list[PoolCostPayload] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1000)


class KeyBillingProfilePayload(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    multiplier: str | None = Field(default=None, max_length=40)
    reason: str | None = Field(default=None, max_length=1000)


def _client_address(request: Request) -> str:
    forwarded = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-real-ip")
        or request.headers.get("x-forwarded-for", "").split(",", 1)[0]
    ).strip()
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


def _manual_usage_to_nano(value: str) -> int:
    try:
        amount = Decimal(value.strip())
        quantized = amount.quantize(Decimal("0.000000001"))
    except (InvalidOperation, ValueError) as exc:
        raise BillingError("原始等效用量格式无效") from exc
    if not amount.is_finite():
        raise BillingError("原始等效用量必须是有限数值")
    if amount != quantized:
        raise BillingError("原始等效用量最多保留九位小数")
    amount_nano_usd = int(quantized * NANO_USD)
    if amount_nano_usd == 0:
        raise BillingError("原始等效用量不能为零")
    if abs(amount_nano_usd) > 9_223_372_036_854_775_807:
        raise BillingError("原始等效用量超出可记录范围")
    return amount_nano_usd


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    service = BillingService(settings, database)
    service.bootstrap()
    app = FastAPI(title="CPA Billing", docs_url=None, redoc_url=None)
    app.state.service = service
    limiter = LoginLimiter()
    quota_refresh_limiter = CooldownLimiter(10)

    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    def current(request: Request) -> tuple[Any, Any]:
        auth = service.get_session(request.cookies.get(USER_COOKIE))
        if auth is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户会话已失效")
        return auth

    def page_current(request: Request) -> tuple[Any, Any] | None:
        return service.get_session(request.cookies.get(USER_COOKIE))

    def admin_current(request: Request) -> Any:
        auth = service.get_admin_session(request.cookies.get(ADMIN_COOKIE))
        if auth is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理会话已失效")
        return auth

    def page_admin_current(request: Request) -> Any | None:
        return service.get_admin_session(request.cookies.get(ADMIN_COOKIE))

    def verify_csrf(request: Request, session: Any) -> None:
        token = request.headers.get("x-csrf-token", "")
        if not token or not constant_equal(token, session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试。")

    def spa_response() -> Response:
        index = FRONTEND_DIST / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html")
        return HTMLResponse(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>CPA Billing</title><body>Frontend bundle is not built.</body></html>",
            status_code=503,
        )

    def set_user_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            USER_COOKIE,
            token,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=settings.session_ttl_seconds,
        )

    @app.exception_handler(BillingDependencyError)
    async def dependency_error(_: Request, exc: BillingDependencyError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(BillingError)
    async def billing_error(_: Request, exc: BillingError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{key: value for key, value in item.items() if key != "input"} for item in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data:; script-src 'self'; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def favicon_response() -> FileResponse:
        candidate = FRONTEND_DIST / "favicon.svg"
        if not candidate.is_file():
            candidate = ROOT / "static" / "favicon.svg"
        return FileResponse(candidate, media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> FileResponse:
        return favicon_response()

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon_svg() -> FileResponse:
        return favicon_response()

    @app.post("/auth/api-key/login")
    async def login(request: Request, payload: UserLoginPayload) -> JSONResponse:
        limiter_key = f"user:{_client_address(request)}"
        limiter.check(limiter_key)
        try:
            authenticated = await asyncio.to_thread(service.authenticate_key, payload.api_key)
        except (httpx.HTTPError, BillingError) as exc:
            LOGGER.warning("API Key authentication dependency failed: %s", type(exc).__name__)
            raise BillingDependencyError("认证服务暂时不可用，请稍后再试。") from exc
        if authenticated is None:
            limiter.failure(limiter_key)
            await asyncio.sleep(0.35)
            raise HTTPException(status_code=401, detail="API Key 无效、已撤销或尚未通过 Telegram 注册。")
        limiter.success(limiter_key)
        user, key = authenticated
        token, csrf = service.create_session(user.telegram_user_id, key.id)
        response = JSONResponse({"ok": True, "telegram_user_id": user.telegram_user_id, "csrf_token": csrf})
        set_user_cookie(response, token)
        response.delete_cookie("billing_session", path="/")
        return response

    @app.post("/auth/logout")
    def logout(request: Request, auth: tuple[Any, Any] = Depends(current)) -> Response:
        verify_csrf(request, auth[0])
        token = request.cookies.get(USER_COOKIE)
        if token:
            service.revoke_session(token)
        response = Response(status_code=204)
        response.delete_cookie(USER_COOKIE, path="/")
        response.delete_cookie("billing_session", path="/")
        return response

    @app.post("/auth/admin/login")
    async def admin_login(request: Request, payload: AdminLoginPayload) -> JSONResponse:
        limiter_key = f"admin:{_client_address(request)}"
        limiter.check(limiter_key)
        if not service.authenticate_admin_token(payload.management_token):
            limiter.failure(limiter_key)
            await asyncio.sleep(0.5)
            raise HTTPException(status_code=401, detail="管理 token 无效。")
        limiter.success(limiter_key)
        token, csrf = service.create_admin_session()
        response = JSONResponse({"ok": True, "is_admin": True, "csrf_token": csrf})
        response.set_cookie(
            ADMIN_COOKIE,
            token,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=settings.session_ttl_seconds,
        )
        return response

    @app.post("/auth/admin/logout")
    def admin_logout(request: Request, auth: Any = Depends(admin_current)) -> Response:
        verify_csrf(request, auth)
        token = request.cookies.get(ADMIN_COOKIE)
        if token:
            service.revoke_admin_session(token)
        response = Response(status_code=204)
        response.delete_cookie(ADMIN_COOKIE, path="/")
        return response

    @app.get("/api/session")
    def api_session(auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        session, user = auth
        return {
            "telegram_user_id": user.telegram_user_id,
            "name": service._user_name(user, user.telegram_user_id),
            "is_admin": False,
            "login_key_id": session.api_key_id,
            "csrf_token": session.csrf_token,
        }

    @app.get("/api/admin/session")
    def api_admin_session(auth: Any = Depends(admin_current)) -> dict[str, Any]:
        return {"is_admin": True, "csrf_token": auth.csrf_token}

    @app.get("/api/dashboard")
    def api_dashboard(cycle: str | None = Query(None), _: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.dashboard(cycle)

    @app.get("/api/users")
    def api_users(cycle: str | None = Query(None), _: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        data = service.dashboard(cycle)
        return {"cycle": data["cycle"], "users": data["rows"]}

    @app.get("/api/users/{user_id}/summary")
    def api_user(user_id: int, cycle: str | None = Query(None),
                 _: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.user_summary(user_id, cycle)

    @app.get("/api/me/keys")
    def api_keys(auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.user_keys(auth[1].telegram_user_id)

    @app.patch("/api/me/keys/{key_id}")
    def api_rename_key(key_id: int, payload: KeyNamePayload, request: Request,
                       auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        verify_csrf(request, auth[0])
        return service.rename_key(auth[1].telegram_user_id, key_id, payload.name)

    @app.post("/api/me/keys/actions")
    async def api_key_action(payload: KeyActionPayload, request: Request,
                             auth: tuple[Any, Any] = Depends(current)) -> JSONResponse:
        verify_csrf(request, auth[0])
        result = await asyncio.to_thread(
            service.execute_web_key_action,
            auth[1].telegram_user_id,
            payload.current_api_key,
            payload.action,
            payload.target_key_id,
        )
        target_is_login_key = payload.target_key_id == auth[0].api_key_id
        session_ended = payload.action == "revoke" and target_is_login_key
        response_payload = {**result, "session_ended": session_ended}
        if payload.action == "reset" and target_is_login_key and result["new_key_id"]:
            token, csrf = service.create_session(auth[1].telegram_user_id, result["new_key_id"])
            response_payload["csrf_token"] = csrf
            response = JSONResponse(response_payload)
            set_user_cookie(response, token)
            return response
        response = JSONResponse(response_payload)
        if session_ended:
            response.delete_cookie(USER_COOKIE, path="/")
        return response

    @app.get("/api/me/usage/filter-options")
    def api_request_options(auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.request_filter_options(auth[1].telegram_user_id)

    @app.get("/api/me/usage/events")
    def api_request_history(
        start: str | None = Query(None),
        end: str | None = Query(None),
        model: list[str] = Query(default=[]),
        tier: str | None = Query(None),
        provider: str | None = Query(None),
        request_status: str | None = Query(None, alias="status"),
        key_id: int | None = Query(None, ge=1),
        failure_code: int | None = Query(None),
        min_tokens: int | None = Query(None, ge=0),
        max_tokens: int | None = Query(None, ge=0),
        min_cost: str | None = Query(None),
        max_cost: str | None = Query(None),
        min_latency: int | None = Query(None, ge=0),
        max_latency: int | None = Query(None, ge=0),
        min_ttft: int | None = Query(None, ge=0),
        max_ttft: int | None = Query(None, ge=0),
        min_tps: float | None = Query(None, ge=0),
        max_tps: float | None = Query(None, ge=0),
        long_context: bool | None = Query(None),
        q: str | None = Query(None, max_length=200),
        sort: str = Query("time_desc"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        auth: tuple[Any, Any] = Depends(current),
    ) -> dict[str, Any]:
        return service.request_history(
            auth[1].telegram_user_id,
            start=start,
            end=end,
            models=model,
            tier=tier,
            provider=provider,
            status=request_status,
            key_id=key_id,
            failure_code=failure_code,
            min_tokens=min_tokens,
            max_tokens=max_tokens,
            min_cost=min_cost,
            max_cost=max_cost,
            min_latency=min_latency,
            max_latency=max_latency,
            min_ttft=min_ttft,
            max_ttft=max_ttft,
            min_tps=min_tps,
            max_tps=max_tps,
            long_context=long_context,
            query_text=q,
            sort=sort,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/admin/usage/filter-options")
    def api_admin_request_options(_: Any = Depends(admin_current)) -> dict[str, Any]:
        return service.request_filter_options(None, all_users=True)

    @app.get("/api/admin/usage/events")
    def api_admin_request_history(
        start: str | None = Query(None),
        end: str | None = Query(None),
        model: list[str] = Query(default=[]),
        tier: str | None = Query(None),
        provider: str | None = Query(None),
        request_status: str | None = Query(None, alias="status"),
        key_id: int | None = Query(None, ge=1),
        failure_code: int | None = Query(None),
        min_tokens: int | None = Query(None, ge=0),
        max_tokens: int | None = Query(None, ge=0),
        min_cost: str | None = Query(None),
        max_cost: str | None = Query(None),
        min_latency: int | None = Query(None, ge=0),
        max_latency: int | None = Query(None, ge=0),
        min_ttft: int | None = Query(None, ge=0),
        max_ttft: int | None = Query(None, ge=0),
        min_tps: float | None = Query(None, ge=0),
        max_tps: float | None = Query(None, ge=0),
        long_context: bool | None = Query(None),
        q: str | None = Query(None, max_length=200),
        sort: str = Query("time_desc"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        _: Any = Depends(admin_current),
    ) -> dict[str, Any]:
        return service.request_history(
            None,
            all_users=True,
            start=start,
            end=end,
            models=model,
            tier=tier,
            provider=provider,
            status=request_status,
            key_id=key_id,
            failure_code=failure_code,
            min_tokens=min_tokens,
            max_tokens=max_tokens,
            min_cost=min_cost,
            max_cost=max_cost,
            min_latency=min_latency,
            max_latency=max_latency,
            min_ttft=min_ttft,
            max_ttft=max_ttft,
            min_tps=min_tps,
            max_tps=max_tps,
            long_context=long_context,
            query_text=q,
            sort=sort,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/rankings")
    def api_rankings(
        range_name: str = Query("24h", alias="range"),
        start: str | None = Query(None),
        end: str | None = Query(None),
        cycle: str | None = Query(None),
        sort: str = Query("cost"),
        _: tuple[Any, Any] = Depends(current),
    ) -> dict[str, Any]:
        return service.ranking_snapshot(range_name, start, end, cycle, sort)

    @app.get("/api/pricing")
    def api_pricing(cycle: str | None = Query(None), _: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return service.pricing_snapshot(cycle)

    @app.get("/api/site/pulse")
    async def api_site_pulse(_: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return await asyncio.to_thread(service.site_pulse)

    @app.get("/api/site/status")
    async def api_site_status(
        range_name: str = Query("24h", alias="range"),
        window: str = Query("60m"),
        start: str | None = Query(None),
        end: str | None = Query(None),
        _: tuple[Any, Any] = Depends(current),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(service.site_status, range_name, window, start, end)

    @app.get("/api/site/accounts")
    async def api_accounts(_: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return await asyncio.to_thread(service.accounts_snapshot)

    @app.post("/api/site/accounts/refresh")
    async def api_refresh_accounts(payload: AccountRefreshPayload, request: Request,
                                   auth: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        verify_csrf(request, auth[0])
        quota_refresh_limiter.check_and_mark(str(auth[1].telegram_user_id))
        return await asyncio.to_thread(service.refresh_account_quotas, payload.account_ids)

    @app.get("/api/site/accounts/{account_id}/refresh")
    async def api_refresh_status(account_id: str, _: tuple[Any, Any] = Depends(current)) -> dict[str, Any]:
        return await asyncio.to_thread(service.account_quota_refresh_status, account_id)

    @app.get("/api/admin/snapshot")
    async def api_admin_snapshot(_: Any = Depends(admin_current)) -> dict[str, Any]:
        try:
            accounts = await asyncio.to_thread(service.accounts_snapshot)
        except BillingDependencyError as exc:
            accounts = {"accounts": [], "inspection": {}, "error": str(exc)}
        return {
            "reconciliation": service.reconciliation(),
            "usage": service.usage_summary(),
            "accounts": accounts,
            "admin": service.admin_snapshot(),
        }

    @app.post("/api/admin/cycles")
    def api_create_cycle(payload: CyclePayload, request: Request, auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        pool_costs = [{
            "pool_id": item.pool_id,
            "fixed_cost_cents": _money_to_cents(item.fixed_cost),
        } for item in payload.pool_costs]
        service.create_cycle(
            payload.name,
            payload.start,
            payload.end,
            _money_to_cents(payload.fixed_cost),
            (payload.waiver or "").strip() or None,
            gradient_rule_id=payload.gradient_rule_id,
            pool_costs=pool_costs or None,
            operator_type="web-admin",
            operator_id="admin-token",
        )
        return {"ok": True}

    @app.put("/api/admin/cycles/{cycle_name}/configuration")
    def api_configure_cycle(cycle_name: str, payload: CycleConfigurationPayload, request: Request,
                            auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        service.configure_cycle(
            cycle_name,
            payload.gradient_rule_id,
            [{
                "pool_id": item.pool_id,
                "fixed_cost_cents": _money_to_cents(item.fixed_cost),
            } for item in payload.pool_costs],
            payload.reason,
        )
        return {"ok": True}

    @app.post("/api/admin/cycles/{cycle_name}/preview")
    def api_preview_cycle(cycle_name: str, request: Request,
                          auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        service.preview_cycle(cycle_name)
        return {"ok": True}

    @app.post("/api/admin/cycles/{cycle_name}/close")
    def api_close_cycle(cycle_name: str, payload: CloseCyclePayload, request: Request,
                        auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        if not payload.confirm_close:
            raise BillingError("关闭账期前必须确认账单将被冻结")
        service.close_cycle(cycle_name, None, payload.confirm_waiver, operator_type="web-admin")
        return {"ok": True}

    @app.post("/api/admin/adjustments")
    def api_adjustment(payload: AdjustmentPayload, request: Request,
                       auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        service.add_adjustment(
            payload.cycle,
            payload.telegram_user_id,
            payload.amount_cents,
            payload.reason,
            None,
            operator_type="web-admin",
        )
        return {"ok": True}

    @app.post("/api/admin/manual-usage-adjustments")
    def api_manual_usage_adjustment(
        payload: ManualUsageAdjustmentPayload,
        request: Request,
        auth: Any = Depends(admin_current),
    ) -> dict[str, Any]:
        verify_csrf(request, auth)
        adjustment_id = service.add_manual_usage_adjustment(
            payload.cycle,
            payload.pool_id,
            payload.telegram_user_id,
            _manual_usage_to_nano(payload.amount_usd),
            payload.reason,
            None,
            operator_type="web-admin",
        )
        return {"ok": True, "id": adjustment_id}

    @app.put("/api/admin/manual-usage-adjustments/{adjustment_id}")
    def api_update_manual_usage_adjustment(
        adjustment_id: int,
        payload: ManualUsageAdjustmentPayload,
        request: Request,
        auth: Any = Depends(admin_current),
    ) -> dict[str, Any]:
        verify_csrf(request, auth)
        updated_id = service.update_manual_usage_adjustment(
            adjustment_id,
            payload.cycle,
            payload.pool_id,
            payload.telegram_user_id,
            _manual_usage_to_nano(payload.amount_usd),
            payload.reason,
            None,
            operator_type="web-admin",
        )
        return {"ok": True, "id": updated_id}

    @app.post("/api/admin/ownership-transfers")
    def api_transfer(payload: OwnershipTransferPayload, request: Request,
                     auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        if not payload.confirm_transfer:
            raise BillingError("变更 Key 归属前必须明确确认")
        service.transfer_key(
            payload.key_id,
            payload.telegram_user_id,
            None,
            payload.reason,
            operator_type="web-admin",
        )
        return {"ok": True}

    @app.post("/api/admin/pools")
    def api_pool(payload: PoolPayload, request: Request, auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        service.create_pool(
            payload.name,
            payload.auth_pattern,
            payload.model_pattern,
            payload.priority,
            operator_type="web-admin",
            operator_id="admin-token",
        )
        return {"ok": True}

    @app.post("/api/admin/pricing-versions/import")
    def api_import_pricing(payload: PricingImportPayload, request: Request,
                           auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        if not payload.confirm_import:
            raise BillingError("导入价格版本前必须明确确认")
        service.import_cpamp_prices(
            payload.name,
            operator_type="web-admin",
            operator_id="admin-token",
            allow_existing=False,
        )
        return {"ok": True}

    @app.post("/api/admin/pricing-versions/sync")
    def api_sync_pricing(payload: PricingSyncPayload, request: Request,
                         auth: Any = Depends(admin_current)) -> dict[str, Any]:
        verify_csrf(request, auth)
        return service.sync_upstream_prices(
            payload.name,
            operator_type="web-admin",
            operator_id="admin-token",
            reason=payload.reason,
        )

    @app.post("/api/admin/gradient-rules")
    def api_create_gradient(payload: GradientRulePayload, request: Request,
                            auth: Any = Depends(admin_current)) -> dict[str, Any]:
        verify_csrf(request, auth)
        rule_id = service.create_gradient_rule(
            payload.name,
            payload.description,
            payload.tiers,
            payload.reason,
        )
        return {"ok": True, "id": rule_id}

    @app.put("/api/admin/gradient-rules/{rule_id}")
    def api_update_gradient(rule_id: int, payload: GradientRulePayload, request: Request,
                            auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        service.update_gradient_rule(
            rule_id,
            payload.name,
            payload.description,
            payload.tiers,
            payload.reason,
        )
        return {"ok": True}

    @app.delete("/api/admin/gradient-rules/{rule_id}")
    def api_delete_gradient(rule_id: int, payload: ReasonPayload, request: Request,
                            auth: Any = Depends(admin_current)) -> dict[str, bool]:
        verify_csrf(request, auth)
        service.delete_gradient_rule(rule_id, payload.reason)
        return {"ok": True}

    @app.patch("/api/admin/keys/{key_id}/billing-profile")
    def api_update_key_billing_profile(key_id: int, payload: KeyBillingProfilePayload, request: Request,
                                       auth: Any = Depends(admin_current)) -> dict[str, Any]:
        verify_csrf(request, auth)
        return service.update_unowned_key_profile(
            key_id,
            payload.name,
            payload.multiplier,
            payload.reason,
        )

    @app.post("/api/admin/cpa-keys/sync")
    def api_sync_cpa_keys(request: Request, auth: Any = Depends(admin_current)) -> dict[str, int]:
        verify_csrf(request, auth)
        return service.sync_cpa_keys()

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> Response:
        if page_current(request):
            return RedirectResponse("/", status_code=303)
        return spa_response()

    @app.get("/admin/login", include_in_schema=False)
    def admin_login_page(request: Request) -> Response:
        if page_admin_current(request):
            return RedirectResponse("/admin", status_code=303)
        return spa_response()

    @app.get("/register", include_in_schema=False)
    def registration_is_disabled() -> None:
        raise HTTPException(status_code=404, detail="Web 不开放注册，请私聊 Telegram Bot 执行 /register。")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_routes(full_path: str, request: Request) -> Response:
        if full_path.startswith("api/") or full_path.startswith("auth/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path == "admin" or full_path.startswith("admin/"):
            if page_admin_current(request) is None:
                return RedirectResponse("/admin/login", status_code=303)
            return spa_response()
        if page_current(request) is None:
            return RedirectResponse("/login", status_code=303)
        return spa_response()

    return app
