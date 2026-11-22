"""API REST (FastAPI) y pantalla de revisión de InvoiceBot.

Pensada para correr detrás de un proxy HTTPS (Azure Container Apps, Caddy/Nginx en
un VPS) o con TLS propio (INVOICEBOT_SERVE_TLS_CERT/_KEY en una VM Windows).
"""

import asyncio
import hashlib
import json
import secrets
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from invoicebot import __version__
from invoicebot.api.security import (
    LoginThrottle,
    Principal,
    authenticate_request,
    client_ip,
    cookie_name,
    is_secure_request,
    principal_dependency,
    require,
)
from invoicebot.application import (
    approvals,
    events,
    integrations,
    payables,
    purchasing,
    review,
    tax,
)
from invoicebot.application.companies import add_company
from invoicebot.application.exchange_rates import save_exchange_rates
from invoicebot.application.reports import build_export, check_month, find_duplicates
from invoicebot.application.users import authenticate, resolve_oidc_user
from invoicebot.application.verify_sunat import verify_sunat
from invoicebot.context import AppContext
from invoicebot.domain import access
from invoicebot.domain.invoice import DOCUMENT_TYPES
from invoicebot.domain.review import CORRECTABLE_FIELDS, ReviewStatus
from invoicebot.errors import (
    ConfigError,
    InvoiceBotError,
    LockedError,
    ReviewError,
    UserError,
    error_message,
)
from invoicebot.infrastructure.db.invoice_repository import month_range
from invoicebot.infrastructure.db.migration import schema_status
from invoicebot.infrastructure.documents.scanner import SUPPORTED_EXTENSIONS
from invoicebot.infrastructure.mail.attachments import safe_file_name
from invoicebot.infrastructure.oidc import OidcError, pkce_pair
from invoicebot.infrastructure.sunat import parse_date

_HERE = Path(__file__).parent


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        # DATETIME2 de SQL Server llega sin zona: es UTC.
        return (
            (value if value.tzinfo else value.replace(tzinfo=UTC))
            .isoformat()
            .replace("+00:00", "Z")
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    raise TypeError(f"No serializable: {type(value).__name__}")


class ApiResponse(JSONResponse):
    """JSON con fechas UTC (sufijo Z), Decimal como número y dataclasses."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content, ensure_ascii=False, default=_default, separators=(",", ":")
        ).encode()


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.path.startswith(("/docs", "/redoc")):
            return response  # Swagger UI usa recursos de su CDN.
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'",
        )
        if request.url.scheme == "https":
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


# --- Modelos de entrada -------------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=300)


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    force: bool = False


class RejectBody(BaseModel):
    reason: str = Field(max_length=1000)


class CorrectBody(BaseModel):
    changes: dict[str, str | int | float | None]
    note: str | None = Field(default=None, max_length=1000)


class ExchangeRateBody(BaseModel):
    date: str = Field(description="AAAA-MM-DD o DD/MM/AAAA")
    sell: float = Field(gt=0)
    buy: float | None = Field(default=None, gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class SunatBody(BaseModel):
    month: str | None = None
    force: bool = False
    limit: int | None = Field(default=50, ge=1, le=1000)


class CompanyBody(BaseModel):
    ruc: str = Field(pattern=r"^\d{11}$")
    name: str = Field(min_length=1, max_length=200)
    retention_agent: bool = False
    good_taxpayer: bool = False


class OrderLineBody(BaseModel):
    line_no: int | None = Field(default=None, ge=1)
    item_code: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0, description="Precio unitario sin IGV")


class PurchaseOrderBody(BaseModel):
    po_number: str = Field(min_length=1, max_length=40)
    supplier_ruc: str = Field(pattern=r"^\d{11}$")
    supplier_name: str | None = Field(default=None, max_length=300)
    company_ruc: str | None = Field(default=None, pattern=r"^\d{11}$")
    currency: str = Field(default="PEN", pattern=r"^[A-Z]{3}$")
    issue_date: str | None = None
    cost_center: str | None = Field(default=None, max_length=30)
    requester: str | None = Field(default=None, max_length=150)
    requires_receipt: bool = True
    status: str = Field(default="open", pattern="^(open|closed|cancelled)$")
    lines: list[OrderLineBody] = Field(min_length=1, max_length=500)


class ReceiptLineBody(BaseModel):
    line_no: int = Field(ge=1, description="Línea de la orden de compra")
    quantity: Decimal = Field(gt=0)


class GoodsReceiptBody(BaseModel):
    receipt_number: str = Field(min_length=1, max_length=40)
    supplier_ruc: str = Field(pattern=r"^\d{11}$")
    company_ruc: str | None = Field(default=None, pattern=r"^\d{11}$")
    po_number: str | None = Field(default=None, max_length=40)
    receipt_date: str | None = None
    despatch_reference: str | None = Field(default=None, max_length=40)
    amount: Decimal | None = Field(default=None, ge=0, description="Valor sin IGV (sin líneas)")
    lines: list[ReceiptLineBody] = Field(default_factory=list, max_length=500)


class MatchBody(BaseModel):
    po_number: str | None = Field(default=None, max_length=40)


class AccountingBody(BaseModel):
    account_code: str | None = Field(default=None, max_length=20)
    cost_center: str | None = Field(default=None, max_length=30)


class DecisionBody(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class ApprovalRuleBody(BaseModel):
    level: int = Field(default=1, ge=1, le=9)
    approver: str | None = Field(default=None, max_length=40)
    company_ruc: str | None = Field(default=None, pattern=r"^\d{11}$")
    cost_center: str | None = Field(default=None, max_length=30)
    supplier_ruc: str | None = Field(default=None, pattern=r"^\d{11}$")
    min_amount: Decimal = Field(default=Decimal(0), ge=0, description="En soles")
    max_amount: Decimal | None = Field(default=None, gt=0, description="En soles (exclusivo)")
    sla_hours: int = Field(default=48, ge=1, le=24 * 30)
    escalate_to: str | None = Field(default=None, max_length=40)


class DelegationBody(BaseModel):
    username: str | None = Field(
        default=None, max_length=40, description="Solo un admin delega por otro"
    )
    delegate: str = Field(max_length=40)
    starts_on: date
    ends_on: date
    reason: str | None = Field(default=None, max_length=300)


class WebhookBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(pattern=r"^https?://", max_length=500)
    events: str = Field(default="*", max_length=500)
    company_ruc: str | None = Field(default=None, pattern=r"^\d{11}$")


class DepositBody(BaseModel):
    invoice_id: int
    constancia: str = Field(pattern=r"^\d{1,20}$")
    deposit_date: date
    amount: Decimal = Field(gt=0, description="Soles")


class SupplierStatusBody(BaseModel):
    good_taxpayer: bool | None = None
    retention_agent: bool | None = None
    perception_agent: bool | None = None


class PaymentBody(BaseModel):
    invoice_id: int | None = None
    supplier_ruc: str | None = Field(default=None, pattern=r"^\d{11}$")
    series: str | None = Field(default=None, max_length=10)
    number: str | None = Field(default=None, max_length=20)
    amount: Decimal = Field(gt=0, description="En la moneda del comprobante")
    paid_on: date
    reference: str | None = Field(default=None, max_length=60, description="Número de operación")


class CostCenterBody(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=150)
    destination_account: str | None = Field(default=None, max_length=20)


class JobBody(BaseModel):
    task: str = Field(max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


def _idempotency(principal: Principal, key: str | None) -> str | None:
    """Clave por cliente: dos sistemas pueden usar el mismo valor sin pisarse."""
    if not key:
        return None
    if len(key) > 200:
        raise HTTPException(400, "Idempotency-Key admite hasta 200 caracteres")
    digest = hashlib.sha256(f"{principal.signature}|{key}".encode()).hexdigest()
    return f"api:{digest[:48]}"


_FINAL_JOB_STATUSES = ("succeeded", "failed", "cancelled")

_SSO_ERRORS = {
    "sso_cancelled": "Se canceló el inicio de sesión.",
    "sso_expired": "El inicio de sesión tardó demasiado: vuelve a intentarlo.",
    "sso_failed": "El proveedor de identidad no confirmó tu cuenta.",
    "sso_no_user": "Tu cuenta no tiene acceso a InvoiceBot: pide a un administrador que te dé de alta.",
}


def create_app(context: AppContext | None = None) -> FastAPI:
    ctx = context or AppContext()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            ctx.ensure_schema()
        except Exception as error:  # La API arranca igual; /readyz informa el problema.
            ctx.logger.error("la base de datos no está lista: %s", error_message(error))
        yield
        ctx.close()

    docs = ctx.config.serve.api_docs
    app = FastAPI(
        title="InvoiceBot API",
        version=__version__,
        description="RPA de comprobantes de pago peruanos: XML UBL, PDF, OCR e IA, validación "
        "contable, SUNAT y SQL Server.",
        lifespan=lifespan,
        default_response_class=ApiResponse,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.context = ctx
    app.state.throttle = LoginThrottle()
    app.state.run_thread = None
    app.add_middleware(SecurityHeaders)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

    @app.exception_handler(InvoiceBotError)
    async def invoicebot_error(_: Request, error: InvoiceBotError) -> Response:
        status = 400 if isinstance(error, ReviewError | UserError | ConfigError) else 500
        if isinstance(error, LockedError):
            status = 409
        if status == 500:
            ctx.logger.error("error en la API: %s", error.message)
        return ApiResponse({"detail": error.message, "code": error.code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, error: RequestValidationError) -> Response:
        return ApiResponse(
            {"detail": "Datos no válidos", "errors": error.errors()}, status_code=422
        )

    Auth = Annotated[Principal, Depends(principal_dependency)]

    def need(permission: str) -> Callable[[Principal], Principal]:
        checker = require(permission)

        def dependency(principal: Auth) -> Principal:
            return checker(principal)

        return dependency

    Reader = Annotated[Principal, Depends(need(access.INVOICES_READ))]
    Reviewer = Annotated[Principal, Depends(need(access.REVIEW_WRITE))]
    Uploader = Annotated[Principal, Depends(need(access.DOCUMENTS_WRITE))]
    Exporter = Annotated[Principal, Depends(need(access.EXPORTS_READ))]
    FxWriter = Annotated[Principal, Depends(need(access.FX_WRITE))]
    SunatWriter = Annotated[Principal, Depends(need(access.SUNAT_WRITE))]
    Runner = Annotated[Principal, Depends(need(access.RUNS_WRITE))]
    JobsReader = Annotated[Principal, Depends(need(access.JOBS_READ))]
    JobsWriter = Annotated[Principal, Depends(need(access.JOBS_WRITE))]
    AuditReader = Annotated[Principal, Depends(need(access.AUDIT_READ))]
    Admin = Annotated[Principal, Depends(need(access.ADMIN))]

    # --- Salud -----------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        try:
            status = schema_status(ctx.db)
            ready = not status["pending"] and not status["ahead"]
        except Exception as error:
            return ApiResponse({"status": "error", "detail": error_message(error)}, status_code=503)
        queue: dict[str, str] = {"backend": ctx.config.queue.backend}
        try:
            queue["detail"] = ctx.queue.ping()
        except Exception as error:
            ready = False
            queue["error"] = error_message(error)
        return ApiResponse(
            {"status": "ok" if ready else "not_ready", "schema": status, "queue": queue},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
    def metrics(_: JobsReader) -> Response:
        """Métricas en formato Prometheus (requiere una clave de API de lectura)."""
        lines = [
            "# HELP invoicebot_info Versión de InvoiceBot.",
            "# TYPE invoicebot_info gauge",
            f'invoicebot_info{{version="{__version__}",queue="{ctx.config.queue.backend}"}} 1',
            "# HELP invoicebot_jobs Trabajos por tarea y estado (terminados: últimas 24 h).",
            "# TYPE invoicebot_jobs gauge",
        ]
        repo = ctx.job_repository
        for row in repo.counts():
            lines.append(
                f'invoicebot_jobs{{task="{row["task"]}",status="{row["status"]}"}} {row["n"]}'
            )
        oldest = repo.oldest_waiting_seconds()
        lines += [
            "# HELP invoicebot_job_oldest_wait_seconds Espera del trabajo listo más antiguo.",
            "# TYPE invoicebot_job_oldest_wait_seconds gauge",
            f"invoicebot_job_oldest_wait_seconds {oldest or 0:.3f}",
        ]
        try:
            sizes = ctx.queue.sizes()
        except Exception as error:
            ctx.logger.warning("métricas de Redis no disponibles: %s", error_message(error))
            sizes = {}
        if sizes:
            lines += [
                "# HELP invoicebot_queue_messages Mensajes en Redis por cola y estado.",
                "# TYPE invoicebot_queue_messages gauge",
            ]
            for queue_name, values in sizes.items():
                for state, value in values.items():
                    lines.append(
                        f'invoicebot_queue_messages{{queue="{queue_name}",state="{state}"}} {value}'
                    )
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # --- Pantalla de revisión --------------------------------------------------------

    @app.get("/", include_in_schema=False, response_model=None)
    def index(request: Request) -> Response:
        try:
            principal = authenticate_request(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "review.html",
            {"version": __version__, "user": principal, "csrf": principal.csrf},
        )

    @app.get("/login", include_in_schema=False, response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None) -> Response:
        oidc = ctx.config.oidc
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "version": __version__,
                "sso": oidc.label if oidc.enabled else None,
                "error": _SSO_ERRORS.get(error or "", ""),
            },
        )

    def _start_session(
        request: Request, user: dict[str, Any], *, same_site: str = "strict"
    ) -> tuple[Response, str]:
        """Crea la sesión y su cookie. Tras volver del proveedor de identidad la cookie debe
        ser Lax: con Strict, el navegador no la enviaría en la redirección que sigue."""
        ip = client_ip(request)
        hours = ctx.config.serve.session_hours
        token, csrf = ctx.users.create_session(user["id"], hours=hours, ip=ip)
        ctx.logger.info(
            "acceso a la pantalla de revisión", extra={"username": user["username"], "ip": ip}
        )
        secure = is_secure_request(request, ctx.config.serve.cookie_secure)
        response = ApiResponse({"ok": True, "csrf": csrf, "user": user["username"]})
        response.set_cookie(
            cookie_name(secure),
            token,
            max_age=hours * 3600,
            httponly=True,
            samesite="lax" if same_site == "lax" else "strict",
            secure=secure,
            path="/",
        )
        return response, token

    @app.get("/auth/oidc/login", include_in_schema=False)
    def oidc_login(request: Request) -> Response:
        """Redirige al proveedor (Microsoft, Google…) con PKCE, state y nonce."""
        if not ctx.config.oidc.enabled:
            raise HTTPException(404, "Inicio de sesión único no configurado")
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        redirect_uri = ctx.config.oidc.redirect_url or str(request.url_for("oidc_callback"))
        ctx.users.save_oidc_request(
            state=state, nonce=nonce, code_verifier=verifier, redirect_uri=redirect_uri
        )
        url = ctx.oidc.authorization_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce, code_challenge=challenge
        )
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/oidc/callback", include_in_schema=False, name="oidc_callback")
    def oidc_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        if error or not code or not state:
            return RedirectResponse("/login?error=sso_cancelled", status_code=303)
        pending = ctx.users.pop_oidc_request(state)
        if pending is None:
            return RedirectResponse("/login?error=sso_expired", status_code=303)
        settings = ctx.config.oidc
        try:
            tokens = ctx.oidc.exchange(
                code=code,
                redirect_uri=pending["redirect_uri"],
                code_verifier=pending["code_verifier"],
            )
            identity = ctx.oidc.verify_id_token(tokens["id_token"], nonce=pending["nonce"])
        except OidcError as failure:
            ctx.logger.warning("inicio de sesión único rechazado: %s", failure.message)
            return RedirectResponse("/login?error=sso_failed", status_code=303)
        user = resolve_oidc_user(
            ctx.users,
            subject=identity.subject,
            email=identity.email,
            name=identity.name,
            email_verified=identity.email_verified,
            allowed_domains=settings.allowed_domains,
            auto_create_role=settings.auto_create_role,
        )
        if user is None:
            ctx.logger.warning(
                "inicio de sesión único sin usuario", extra={"email": identity.email}
            )
            return RedirectResponse("/login?error=sso_no_user", status_code=303)
        session_response, _token = _start_session(request, user, same_site="lax")
        redirect = RedirectResponse("/", status_code=303)
        for header, value in session_response.raw_headers:
            if header.lower() == b"set-cookie":
                redirect.raw_headers.append((header, value))
        return redirect

    @app.post("/api/v1/auth/login", tags=["auth"])
    def login(body: LoginBody, request: Request) -> Response:
        ip = client_ip(request)
        username = body.username.strip().lower()
        keys = [f"ip:{ip}", f"user:{username}"]
        throttle: LoginThrottle = app.state.throttle
        if throttle.is_locked(keys):
            ctx.logger.warning(
                "acceso bloqueado temporalmente", extra={"username": username, "ip": ip}
            )
            raise HTTPException(429, "Demasiados intentos fallidos. Espera 15 minutos.")
        user = authenticate(ctx.users, username, body.password)
        if not user:
            throttle.record_failure(keys)
            ctx.logger.warning("acceso rechazado", extra={"username": username, "ip": ip})
            raise HTTPException(401, "Usuario o contraseña incorrectos")
        throttle.clear(keys)
        response, _token = _start_session(request, user)
        return response

    @app.post("/api/v1/auth/logout", tags=["auth"])
    def logout(request: Request, principal: Auth) -> Response:
        ctx.users.delete_session(principal.session_token)
        secure = is_secure_request(request, ctx.config.serve.cookie_secure)
        response = ApiResponse({"ok": True})
        response.delete_cookie(
            cookie_name(secure), path="/", secure=secure, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/v1/auth/me", tags=["auth"])
    def me(principal: Auth) -> dict[str, Any]:
        return {
            "kind": principal.kind,
            "name": principal.name,
            "display_name": principal.display_name,
            "scope": principal.scope,
            "role": principal.role,
            "permissions": sorted(principal.permissions),
            "companies": ctx.companies.list_companies(
                ids=list(principal.company_ids) if principal.company_ids is not None else None
            ),
            "all_companies": principal.company_ids is None,
        }

    # --- Empresas y auditoría ---------------------------------------------------------

    @app.get("/api/v1/companies", tags=["empresas"])
    def list_companies(principal: Reader) -> dict[str, Any]:
        ids = list(principal.company_ids) if principal.company_ids is not None else None
        return {"items": ctx.companies.list_companies(ids=ids)}

    @app.post("/api/v1/companies", tags=["empresas"], status_code=201)
    def create_company(body: CompanyBody, principal: Admin) -> dict[str, Any]:
        company = add_company(
            ctx.companies,
            ruc=body.ruc,
            name=body.name,
            retention_agent=body.retention_agent,
            good_taxpayer=body.good_taxpayer,
        )
        ctx.logger.info("empresa creada", extra={"by": principal.signature, "ruc": body.ruc})
        return company

    @app.get("/api/v1/invoices/{invoice_id}/history", tags=["auditoría"])
    def invoice_history(invoice_id: int, _: AuditReader) -> dict[str, Any]:
        """Cada versión de la factura (tabla temporal de SQL Server) y las acciones de revisión."""
        detail = review.get_invoice_detail(ctx.review_deps(), invoice_id)
        return {
            "invoice_id": invoice_id,
            "versions": ctx.repository.invoice_history(invoice_id),
            "actions": detail.get("actions", []),
        }

    # --- Consultas -------------------------------------------------------------------

    @app.get("/api/v1/meta", tags=["consultas"])
    def meta(principal: Reader) -> dict[str, Any]:
        return {
            "version": __version__,
            "reviewer": principal.signature,
            "fields": CORRECTABLE_FIELDS,
            "document_types": DOCUMENT_TYPES,
        }

    @app.get("/api/v1/status", tags=["consultas"])
    def status(_: Reader) -> dict[str, Any]:
        repo = ctx.repository
        return {
            "summary": repo.get_status_summary(),
            "runs": repo.list_bot_runs(10),
            "database": ctx.db.describe(),
        }

    @app.get("/api/v1/queue", tags=["revisión"])
    def queue(_: Reader, view: str = "pending") -> dict[str, Any]:
        repo = ctx.repository
        summary = repo.get_status_summary()
        counts = {
            "pending": len(repo.list_invoices_needing_review()),
            "approved": summary["review"].get("approved", 0),
            "rejected": summary["review"].get("rejected", 0),
            "failed": summary["documents"].get("failed", 0),
            "needs_ocr": summary["documents"].get("needs_ocr", 0),
        }
        if view == "pending":
            data = review.get_review_queue(ctx.review_deps())
            return {
                "view": view,
                "counts": counts,
                "items": data["invoices"],
                "failed": data["failed"],
                "needs_ocr": data["needs_ocr"],
            }
        if view not in ReviewStatus.ALL:
            raise HTTPException(400, "Vista desconocida")
        return {"view": view, "counts": counts, "items": repo.list_invoices_by_review_status(view)}

    @app.get("/api/v1/invoices", tags=["consultas"])
    def list_invoices(
        _: Reader,
        month: str | None = None,
        validation_status: str | None = None,
        review_status: str | None = None,
        supplier_ruc: str | None = None,
        q: Annotated[str | None, Query(max_length=100)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        check_month(month)
        rows, total = ctx.repository.search_invoices(
            month=month,
            validation_status=validation_status,
            review_status=review_status,
            supplier_ruc=supplier_ruc,
            query=q,
            limit=limit,
            offset=offset,
        )
        return {"items": rows, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/invoices/{invoice_id}", tags=["consultas"])
    def invoice_detail(invoice_id: int, _: Reader) -> dict[str, Any]:
        return review.get_invoice_detail(ctx.review_deps(), invoice_id)

    @app.get("/api/v1/invoices/{invoice_id}/document", tags=["consultas"], response_model=None)
    def invoice_document(invoice_id: int, _: Reader) -> Response:
        detail = ctx.repository.get_invoice_detail(invoice_id)
        if not detail:
            raise HTTPException(404, "No existe la factura")
        path = Path(detail["file_path"])
        if not path.is_file():
            raise HTTPException(404, "El archivo original ya no está en su ruta")
        # Un XML puede incrustar XHTML con scripts: se sirve como texto.
        media = (
            "text/plain; charset=utf-8"
            if detail["mime_type"] == "application/xml"
            else detail["mime_type"]
        )
        return FileResponse(
            path,
            media_type=media,
            filename=detail["file_name"],
            content_disposition_type="inline",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'",
                "Cache-Control": "private, max-age=300",
            },
        )

    @app.get("/api/v1/duplicates", tags=["consultas"])
    def duplicates(_: Reader) -> dict[str, Any]:
        return find_duplicates(ctx.review_deps())

    @app.get("/api/v1/exports", tags=["consultas"], response_model=None)
    def export(
        _: Exporter,
        month: str | None = None,
        format: Annotated[str, Query(pattern="^(csv|xlsx)$")] = "xlsx",
        include_errors: bool = False,
        include_duplicates: bool = False,
        supplier_ruc: str | None = None,
        currency: str | None = None,
        delimiter: Annotated[str, Query(pattern="^[,;|\t]$")] = ",",
        bom: bool = False,
    ) -> Response:
        result = build_export(
            ctx.review_deps(),
            month=month,
            fmt=format,
            include_errors=include_errors,
            include_duplicates=include_duplicates,
            supplier_ruc=supplier_ruc,
            currency=currency,
            delimiter=delimiter,
            bom=bom,
        )
        return Response(
            result.content,
            media_type=result.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{result.file_name}"',
                "X-Rows": str(result.rows),
            },
        )

    # --- Revisión --------------------------------------------------------------------

    @app.post("/api/v1/invoices/{invoice_id}/approve", tags=["revisión"])
    def approve(invoice_id: int, body: NoteBody, principal: Reviewer) -> dict[str, Any]:
        result = review.approve_invoice(
            ctx.review_deps(),
            invoice_id,
            user=principal.signature,
            note=body.note,
            force=body.force,
        )
        ctx.after_review_approved(int(result["invoice_id"]))
        return result

    @app.post("/api/v1/invoices/{invoice_id}/reject", tags=["revisión"])
    def reject(invoice_id: int, body: RejectBody, principal: Reviewer) -> dict[str, Any]:
        result = review.reject_invoice(
            ctx.review_deps(), invoice_id, user=principal.signature, reason=body.reason
        )
        ctx.after_review_rejected(invoice_id, body.reason)
        return result

    @app.post("/api/v1/invoices/{invoice_id}/reopen", tags=["revisión"])
    def reopen(invoice_id: int, body: NoteBody, principal: Reviewer) -> dict[str, Any]:
        return review.reopen_invoice(
            ctx.review_deps(), invoice_id, user=principal.signature, note=body.note
        )

    @app.post("/api/v1/invoices/{invoice_id}/correct", tags=["revisión"])
    def correct(invoice_id: int, body: CorrectBody, principal: Reviewer) -> dict[str, Any]:
        if not body.changes:
            raise HTTPException(400, "Faltan los cambios")
        return review.correct_invoice(
            ctx.review_deps(),
            invoice_id,
            changes=list(body.changes.items()),
            user=principal.signature,
            note=body.note,
        )

    @app.post("/api/v1/issues/{issue_id}/resolve", tags=["revisión"])
    def resolve(issue_id: int, body: NoteBody, principal: Reviewer) -> dict[str, Any]:
        return review.resolve_issue(
            ctx.review_deps(), issue_id, user=principal.signature, note=body.note
        )

    # --- Documentos (integración RPA) ------------------------------------------------

    @app.post(
        "/api/v1/documents",
        tags=["documentos"],
        status_code=201,
        responses={202: {"description": "Recibidos y encolados (cola Redis)"}},
    )
    async def upload_documents(
        principal: Uploader,
        files: Annotated[
            list[UploadFile],
            File(description="XML, PDF, PNG o JPG; el XML y su PDF juntos se vinculan"),
        ],
        process: Annotated[bool, Form()] = True,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> Response:
        """Recibe comprobantes de otro sistema (email-bot, ERP, un escáner) y los procesa.

        Con la cola Redis responde 202 con el trabajo (`GET /api/v1/jobs/{id}`); sin Redis,
        201 con el resumen. Repetir la petición con la misma cabecera `Idempotency-Key`
        devuelve el mismo trabajo sin volver a guardar los archivos.
        """
        from starlette.concurrency import run_in_threadpool

        idem = _idempotency(principal, idempotency_key)
        if idem:
            previous = await run_in_threadpool(ctx.job_repository.get_by_idempotency_key, idem)
            if previous is not None:
                return ApiResponse(
                    {
                        "job": previous,
                        "processed": previous["status"] == "succeeded",
                        "replayed": True,
                    },
                    status_code=200,
                )
        limit = ctx.config.serve.upload_max_mb * 1024 * 1024
        if not files or len(files) > 50:
            raise HTTPException(400, "Envía entre 1 y 50 archivos")
        batch = (
            Path(ctx.config.upload_dir)
            / datetime.now(UTC).strftime("%Y-%m")
            / (datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4))
        )
        saved: list[str] = []
        for upload in files:
            name = safe_file_name(upload.filename or "documento")
            if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise HTTPException(
                    415, f"Tipo de archivo no soportado: {name} (usa XML, PDF, PNG o JPG)"
                )
            content = await upload.read(limit + 1)
            if len(content) > limit:
                raise HTTPException(413, f"{name} supera {ctx.config.serve.upload_max_mb} MB")
            batch.mkdir(parents=True, exist_ok=True)
            target = batch / name
            n = 2
            while target.exists():
                target = batch / f"{Path(name).stem}-{n}{Path(name).suffix}"
                n += 1
            target.write_bytes(content)
            saved.append(str(target))
        ctx.logger.info(
            "documentos recibidos por la API",
            extra={"by": principal.signature, "files": len(saved)},
        )
        if not process:
            return ApiResponse(
                {"folder": str(batch), "files": saved, "processed": False}, status_code=201
            )
        result = await run_in_threadpool(
            ctx.jobs.enqueue,
            "documents.scan",
            {"root": str(batch), "recursive": False},
            idempotency_key=idem,
            created_by=principal.signature,
        )
        job = await run_in_threadpool(ctx.job_repository.get, result.id) or result.job
        body: dict[str, Any] = {"folder": str(batch), "files": saved, "job": job}
        if job["status"] == "succeeded":
            body.update(processed=True, summary=job.get("result"))
            return ApiResponse(body, status_code=201)
        body.update(processed=False, status_url=f"/api/v1/jobs/{job['id']}")
        return ApiResponse(body, status_code=202)

    # --- Tipo de cambio --------------------------------------------------------------

    @app.get("/api/v1/fx", tags=["tipo de cambio"])
    def list_fx(_: Reader, month: str | None = None, currency: str = "USD") -> dict[str, Any]:
        today = ctx.config.today()
        month = check_month(month) or today[:7]
        start, following = month_range(month)
        end = following - timedelta(days=1)
        items = ctx.repository.list_exchange_rates(
            currency=currency, start=start.isoformat(), end=end.isoformat()
        )
        return {"month": month, "items": items}

    @app.post("/api/v1/fx", tags=["tipo de cambio"])
    def set_fx(body: ExchangeRateBody, _: FxWriter) -> dict[str, Any]:
        day = parse_date(body.date)
        if not day:
            raise HTTPException(400, "Fecha no válida (usa AAAA-MM-DD o DD/MM/AAAA)")
        rate = {
            "currency": body.currency,
            "date": day,
            "buy": body.buy,
            "sell": body.sell,
            "source": "manual",
        }
        return save_exchange_rates(ctx.review_deps(), [rate])

    # --- SUNAT y ciclos del bot ------------------------------------------------------

    @app.post("/api/v1/sunat/verify", tags=["sunat"])
    def sunat_verify(body: SunatBody, _: SunatWriter) -> dict[str, Any]:
        check_month(body.month)
        summary = verify_sunat(
            ctx.review_deps(),
            client=ctx.sunat_client(),
            month=body.month,
            force=body.force,
            limit=body.limit,
        )
        return summary.as_dict()

    @app.get("/api/v1/runs", tags=["bot"])
    def runs(_: JobsReader, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict[str, Any]:
        return {"items": ctx.repository.list_bot_runs(limit)}

    @app.get("/api/v1/runs/{run_id}", tags=["bot"])
    def run_detail(run_id: int, _: JobsReader) -> dict[str, Any]:
        row = ctx.repository.get_bot_run(run_id)
        if not row:
            raise HTTPException(404, "No existe la ejecución")
        return row

    @app.post("/api/v1/runs", tags=["bot"], status_code=202)
    def start_run(principal: Runner) -> dict[str, Any]:
        """Lanza un ciclo en segundo plano (el candado de SQL Server evita dos a la vez).

        Con la cola Redis el ciclo es un trabajo `bot.cycle` que ejecuta un worker.
        """
        if ctx.jobs.backend != "inline":
            result = ctx.jobs.enqueue(
                "bot.cycle",
                {"trigger": "api"},
                dedupe_key="api:bot.cycle",
                created_by=principal.signature,
            )
            if not result.created:
                raise HTTPException(409, f"Ya hay un ciclo en cola (trabajo #{result.id})")
            return {
                "status": "queued",
                "job_id": result.id,
                "status_url": f"/api/v1/jobs/{result.id}",
            }
        current: threading.Thread | None = app.state.run_thread
        if current is not None and current.is_alive():
            raise HTTPException(409, "Ya hay un ciclo en curso lanzado desde la API")
        if any(r["status"] == "running" for r in ctx.repository.list_bot_runs(5)):
            raise HTTPException(409, "Ya hay un ciclo en curso")

        def work() -> None:
            try:
                ctx.run_cycle(trigger="api")
            except LockedError as error:
                ctx.logger.info("ciclo omitido: %s", error.message)
            except Exception:
                ctx.logger.exception("el ciclo lanzado desde la API falló")

        thread = threading.Thread(target=work, name="invoicebot-run", daemon=True)
        app.state.run_thread = thread
        thread.start()
        ctx.logger.info("ciclo lanzado desde la API", extra={"by": principal.signature})
        return {"status": "started"}

    # --- Compras: órdenes, recepciones, match y datos contables ------------------------

    Purchasing = Annotated[Principal, Depends(need(access.PURCHASING_WRITE))]
    Approver = Annotated[Principal, Depends(need(access.APPROVALS_WRITE))]

    @app.post("/api/v1/purchase-orders", tags=["compras"], status_code=201)
    def save_purchase_order(body: PurchaseOrderBody, _: Purchasing) -> dict[str, Any]:
        """Crea o reemplaza una orden de compra (desde el ERP). Importes sin IGV."""
        result = purchasing.upsert_purchase_order(ctx, body.model_dump(mode="json"), source="api")
        return result

    @app.get("/api/v1/purchase-orders", tags=["compras"])
    def list_purchase_orders(
        principal: Reader,
        supplier_ruc: str | None = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        items = purchasing.list_orders(
            ctx,
            company_ids=principal.company_ids,
            supplier_ruc=supplier_ruc,
            status=status,
            limit=limit,
        )
        return {"items": items}

    @app.get("/api/v1/purchase-orders/{order_id}", tags=["compras"])
    def purchase_order_detail(order_id: int, principal: Reader) -> dict[str, Any]:
        order = purchasing.get_order(ctx, order_id, company_ids=principal.company_ids)
        if order is None:
            raise HTTPException(404, "No existe la orden de compra")
        return order

    @app.post("/api/v1/goods-receipts", tags=["compras"], status_code=201)
    def save_goods_receipt(body: GoodsReceiptBody, _: Purchasing) -> dict[str, Any]:
        """Nota de ingreso, guía de remisión o conformidad de servicio (también desde document-bot)."""
        result = purchasing.record_goods_receipt(ctx, body.model_dump(mode="json"), source="api")
        # Las facturas de esa orden que esperaban la recepción se vuelven a evaluar.
        if result["order_id"]:
            for match in purchasing.invoices_waiting(ctx, int(result["order_id"])):
                ctx.jobs.enqueue(
                    "purchasing.match", {"invoice_id": match}, dedupe_key=f"match:{match}"
                )
        return result

    @app.get("/api/v1/invoices/{invoice_id}/match", tags=["compras"])
    def invoice_match(invoice_id: int, _: Reader) -> dict[str, Any]:
        match = purchasing.get_match(ctx, invoice_id)
        if match is None:
            raise HTTPException(404, "La factura no tiene match todavía")
        return match

    @app.post("/api/v1/invoices/{invoice_id}/match", tags=["compras"])
    def rematch_invoice(invoice_id: int, body: MatchBody, _: Purchasing) -> dict[str, Any]:
        """Vuelve a hacer el match; con `po_number` vincula la factura a esa orden a mano."""
        if ctx.repository.get_invoice_detail(invoice_id) is None:
            raise HTTPException(404, "No existe la factura")
        return purchasing.run_match(ctx, invoice_id, po_number=body.po_number)

    @app.patch("/api/v1/invoices/{invoice_id}/accounting", tags=["compras"])
    def invoice_accounting(invoice_id: int, body: AccountingBody, _: Reviewer) -> dict[str, Any]:
        """Cuenta contable y centro de costo; se aprenden para el proveedor."""
        return purchasing.set_accounting(
            ctx, invoice_id, account_code=body.account_code, cost_center=body.cost_center
        )

    # --- Aprobación de pago ------------------------------------------------------------------

    @app.get("/api/v1/approvals", tags=["aprobaciones"])
    def list_approvals(
        principal: Reader,
        status: str | None = "pending",
        mine: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        return {
            "items": approvals.list_requests(
                ctx, status=status or None, approver=principal.name if mine else None, limit=limit
            )
        }

    @app.post("/api/v1/approvals/{approval_id}/approve", tags=["aprobaciones"])
    def approve_payment(
        approval_id: int, body: DecisionBody, principal: Approver
    ) -> dict[str, Any]:
        return approvals.decide(
            ctx,
            approval_id,
            approve=True,
            user=principal.name,
            comment=body.comment,
            is_admin=principal.can(access.ADMIN),
        )

    @app.post("/api/v1/approvals/{approval_id}/reject", tags=["aprobaciones"])
    def reject_payment(approval_id: int, body: DecisionBody, principal: Approver) -> dict[str, Any]:
        return approvals.decide(
            ctx,
            approval_id,
            approve=False,
            user=principal.name,
            comment=body.comment,
            is_admin=principal.can(access.ADMIN),
        )

    @app.get("/approve/{token}", include_in_schema=False, response_class=HTMLResponse)
    def approval_page(request: Request, token: str) -> Response:
        """Página del enlace de un clic (llega por correo): muestra la factura y pide confirmar."""
        item = approvals.by_token(ctx, token)
        return templates.TemplateResponse(
            request,
            "approve.html",
            {"item": item, "token": token, "done": None, "version": __version__},
            status_code=200 if item else 410,
        )

    @app.post("/approve/{token}", include_in_schema=False, response_class=HTMLResponse)
    def approval_decision(
        request: Request,
        token: str,
        decision: Annotated[str, Form(pattern="^(approve|reject)$")],
        comment: Annotated[str | None, Form(max_length=1000)] = None,
    ) -> Response:
        try:
            result = approvals.decide_by_token(
                ctx, token, approve=decision == "approve", comment=comment
            )
        except ReviewError as error:
            return templates.TemplateResponse(
                request,
                "approve.html",
                {
                    "item": None,
                    "token": token,
                    "done": None,
                    "error": error.message,
                    "version": __version__,
                },
                status_code=400,
            )
        return templates.TemplateResponse(
            request,
            "approve.html",
            {"item": None, "token": token, "done": result, "version": __version__},
        )

    @app.get("/api/v1/approval-rules", tags=["aprobaciones"])
    def list_approval_rules(_: Reader) -> dict[str, Any]:
        return {"items": approvals.list_rules(ctx)}

    @app.post("/api/v1/approval-rules", tags=["aprobaciones"], status_code=201)
    def create_approval_rule(body: ApprovalRuleBody, _: Admin) -> dict[str, Any]:
        from invoicebot.application.companies import require_company

        company_id = (
            int(require_company(ctx.companies, body.company_ruc)["id"])
            if body.company_ruc
            else None
        )
        return approvals.add_rule(
            ctx,
            level=body.level,
            approver=body.approver,
            company_id=company_id,
            cost_center=body.cost_center,
            supplier_ruc=body.supplier_ruc,
            min_amount_cents=round(body.min_amount * 100),
            max_amount_cents=round(body.max_amount * 100) if body.max_amount is not None else None,
            sla_hours=body.sla_hours,
            escalate_to=body.escalate_to,
        )

    @app.delete("/api/v1/approval-rules/{rule_id}", tags=["aprobaciones"])
    def delete_approval_rule(rule_id: int, _: Admin) -> dict[str, Any]:
        if not approvals.disable_rule(ctx, rule_id):
            raise HTTPException(404, "No existe la regla")
        return {"id": rule_id, "active": False}

    @app.post("/api/v1/approval-delegations", tags=["aprobaciones"], status_code=201)
    def create_delegation(body: DelegationBody, principal: Approver) -> dict[str, Any]:
        owner = body.username or principal.name
        if owner != principal.name and not principal.can(access.ADMIN):
            raise HTTPException(403, "Solo un administrador delega en nombre de otra persona")
        return approvals.delegate(
            ctx,
            username=owner,
            to=body.delegate,
            starts_on=body.starts_on,
            ends_on=body.ends_on,
            reason=body.reason,
        )

    # --- Cumplimiento tributario (SIRE, detracciones, retenciones) --------------------------

    @app.post("/api/v1/sire/proposals", tags=["tributario"], status_code=201)
    async def upload_sire_proposal(
        principal: Reviewer,
        file: Annotated[UploadFile, File(description="Propuesta del RCE (TXT, CSV o ZIP)")],
        period: Annotated[str, Form(pattern=r"^\d{4}-?\d{2}$")],
        company_ruc: Annotated[str | None, Form(pattern=r"^\d{11}$")] = None,
    ) -> dict[str, Any]:
        """Importa la propuesta del Registro de Compras descargada del SIRE."""
        from starlette.concurrency import run_in_threadpool

        content = await file.read(200 * 1024 * 1024 + 1)
        if len(content) > 200 * 1024 * 1024:
            raise HTTPException(413, "El archivo supera 200 MB")
        suffix = Path(file.filename or "propuesta.txt").suffix.lower() or ".txt"
        folder = Path(ctx.config.data_dir) / "sire"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{period.replace('-', '')}-{secrets.token_hex(4)}{suffix}"
        target.write_bytes(content)
        return await run_in_threadpool(
            tax.import_sire_proposal,
            ctx,
            target,
            period=period,
            company_ruc=company_ruc,
            created_by=principal.signature,
        )

    @app.get("/api/v1/sire/reconciliation", tags=["tributario"], response_model=None)
    def sire_reconciliation(
        _: Reader,
        period: Annotated[str, Query(pattern=r"^\d{4}-?\d{2}$")],
        company_ruc: str | None = None,
        format: Annotated[str, Query(pattern="^(json|xlsx)$")] = "json",
    ) -> Response:
        data = tax.reconcile(ctx, period=period, company_ruc=company_ruc)
        if format == "xlsx":
            return Response(
                tax.reconciliation_xlsx(data),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "content-disposition": f'attachment; filename="sire-{data["period"]}.xlsx"'
                },
            )
        return ApiResponse(data)

    @app.get("/api/v1/detractions/pending", tags=["tributario"])
    def detractions_pending(principal: Reader) -> dict[str, Any]:
        return {"items": tax.pending_detractions(ctx, company_ids=principal.company_ids)}

    @app.post("/api/v1/detractions/deposits", tags=["tributario"], status_code=201)
    def detraction_deposit(body: DepositBody, principal: Reviewer) -> dict[str, Any]:
        if ctx.repository.get_invoice_detail(body.invoice_id) is None:
            raise HTTPException(404, "No existe la factura")
        return tax.record_deposit(
            ctx,
            invoice_id=body.invoice_id,
            constancia=body.constancia,
            deposit_date=body.deposit_date,
            amount_cents=round(body.amount * 100),
            created_by=principal.signature,
        )

    @app.get(
        "/api/v1/detractions/payment-file", tags=["tributario"], response_class=PlainTextResponse
    )
    def detraction_file(
        _: Exporter,
        invoice_id: Annotated[list[int], Query()],
        lot: Annotated[str, Query(pattern=r"^\d{6}$")],
        company_ruc: str | None = None,
    ) -> Response:
        content = tax.detraction_payment_file(
            ctx, invoice_ids=invoice_id, lot=lot, company_ruc=company_ruc
        )
        return PlainTextResponse(
            content, headers={"content-disposition": f'attachment; filename="D{lot}.txt"'}
        )

    @app.get("/api/v1/retentions", tags=["tributario"])
    def retentions(
        _: Exporter,
        month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
        company_ruc: str | None = None,
    ) -> dict[str, Any]:
        items = tax.retention_report(ctx, month=month, company_ruc=company_ruc)
        return {
            "month": month,
            "items": items,
            "total_cents": sum(i["retention_cents"] for i in items),
        }

    @app.put("/api/v1/suppliers/{ruc}/tax-status", tags=["tributario"])
    def supplier_status(ruc: str, body: SupplierStatusBody, _: Reviewer) -> dict[str, Any]:
        return tax.set_supplier_status(ctx, ruc, **body.model_dump())

    # --- Contabilidad y pagos ----------------------------------------------------------------

    Treasury = Annotated[Principal, Depends(need(access.PAYMENTS_WRITE))]

    @app.get("/api/v1/accounting/entries", tags=["contabilidad"], response_model=None)
    def accounting_entries(
        principal: Exporter,
        month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
        format: Annotated[str, Query(pattern="^(json|csv|xlsx|concar)$")] = "json",
    ) -> Response:
        """Asientos de provisión (PCGE) de las facturas autorizadas del mes, para el ERP."""
        lines = payables.journal(ctx, month=month, company_ids=principal.company_ids)
        if format == "csv":
            return Response(
                payables.journal_csv(lines),
                media_type="text/csv; charset=utf-8",
                headers={"content-disposition": f'attachment; filename="asientos-{month}.csv"'},
            )
        if format in ("xlsx", "concar"):
            return Response(
                payables.journal_xlsx(lines, concar=format == "concar"),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "content-disposition": f'attachment; filename="asientos-{month}-{format}.xlsx"'
                },
            )
        return ApiResponse({"month": month, "lines": [line.__dict__ for line in lines]})

    @app.post("/api/v1/cost-centers", tags=["contabilidad"], status_code=201)
    def save_cost_center(body: CostCenterBody, _: Admin) -> dict[str, Any]:
        return payables.add_cost_center(
            ctx, code=body.code, name=body.name, destination_account=body.destination_account
        )

    @app.get("/api/v1/payables", tags=["pagos"])
    def list_payables(
        principal: Reader,
        status: str | None = "scheduled",
        until: date | None = None,
    ) -> dict[str, Any]:
        """Partidas por pagar (para bank-bot o tesorería)."""
        return {
            "items": payables.payables(
                ctx, status=status or None, until=until, company_ids=principal.company_ids
            )
        }

    @app.post("/api/v1/payments", tags=["pagos"], status_code=201)
    def confirm_payment(body: PaymentBody, principal: Treasury) -> dict[str, Any]:
        """Pago confirmado (bank-bot al conciliar el cargo). Idempotente por `reference`."""
        if (
            body.invoice_id is not None
            and ctx.repository.get_invoice_detail(body.invoice_id) is None
        ):
            raise HTTPException(404, "No existe la factura")
        return payables.record_payment(
            ctx,
            invoice_id=body.invoice_id,
            supplier_ruc=body.supplier_ruc,
            series=body.series,
            number=body.number,
            amount_cents=round(body.amount * 100),
            paid_on=body.paid_on,
            reference=body.reference,
            source="bank-bot" if principal.kind == "api_key" else "manual",
            created_by=principal.signature,
        )

    # --- Webhooks y eventos ------------------------------------------------------------------

    @app.get("/api/v1/webhooks", tags=["integración"])
    def list_webhooks(_: Admin) -> dict[str, Any]:
        return {"items": integrations.list_webhooks(ctx), "events": events.EVENT_TYPES}

    @app.post("/api/v1/webhooks", tags=["integración"], status_code=201)
    def create_webhook(body: WebhookBody, _: Admin) -> dict[str, Any]:
        """Devuelve el secreto de la firma HMAC una sola vez."""
        return integrations.add_webhook(
            ctx, name=body.name, url=body.url, events=body.events, company_ruc=body.company_ruc
        )

    @app.delete("/api/v1/webhooks/{webhook_id}", tags=["integración"])
    def delete_webhook(webhook_id: int, _: Admin) -> dict[str, Any]:
        if not integrations.disable_webhook(ctx, webhook_id):
            raise HTTPException(404, "No existe el webhook")
        return {"id": webhook_id, "active": False}

    @app.get("/api/v1/events", tags=["integración"])
    def list_events(
        _: AuditReader,
        type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        return {"items": integrations.list_events(ctx, event_type=type, limit=limit)}

    # --- Trabajos en cola ------------------------------------------------------------

    @app.get("/api/v1/tasks", tags=["trabajos"])
    def list_tasks(_: JobsReader) -> dict[str, Any]:
        from invoicebot.application.tasks import registry

        return {
            "backend": ctx.jobs.backend,
            "items": [
                {"name": s.name, "queue": s.queue, "description": s.description}
                for s in sorted(registry.tasks.values(), key=lambda s: s.name)
            ],
        }

    @app.get("/api/v1/jobs", tags=["trabajos"])
    def list_jobs(
        _: JobsReader,
        status: str | None = None,
        task: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        items = ctx.job_repository.list_jobs(status=status, task=task, limit=limit, offset=offset)
        return {"items": items, "limit": limit, "offset": offset}

    @app.post("/api/v1/jobs", tags=["trabajos"], status_code=202)
    def create_job(
        body: JobBody,
        principal: JobsWriter,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Encola una tarea (`GET /api/v1/tasks`) con sus parámetros."""
        result = ctx.jobs.enqueue(
            body.task,
            body.payload,
            idempotency_key=_idempotency(principal, idempotency_key),
            created_by=principal.signature,
        )
        return {"created": result.created, "job": ctx.job_repository.get(result.id) or result.job}

    def _job_or_404(job_id: int) -> dict[str, Any]:
        job = ctx.job_repository.get(job_id)
        if job is None:
            raise HTTPException(404, "No existe el trabajo")
        return job

    @app.get("/api/v1/jobs/{job_id}", tags=["trabajos"])
    def job_detail(job_id: int, _: JobsReader) -> dict[str, Any]:
        job = _job_or_404(job_id)
        job["children"] = ctx.job_repository.children(job_id)
        return job

    @app.get("/api/v1/jobs/{job_id}/events", tags=["trabajos"])
    async def job_events(job_id: int, _: JobsReader, request: Request) -> Response:
        """Progreso en vivo (Server-Sent Events) hasta que el trabajo termina."""
        from starlette.concurrency import run_in_threadpool

        _job_or_404(job_id)

        async def stream() -> AsyncIterator[bytes]:
            last: str | None = None
            for _ in range(600):  # hasta 10 minutos; el cliente puede reconectar
                if await request.is_disconnected():
                    return
                job = await run_in_threadpool(ctx.job_repository.get, job_id)
                if job is None:
                    return
                job["children"] = await run_in_threadpool(ctx.job_repository.children, job_id)
                data = json.dumps(job, ensure_ascii=False, default=_default)
                if data != last:
                    last = data
                    yield f"event: job\ndata: {data}\n\n".encode()
                if job["status"] in _FINAL_JOB_STATUSES and not any(
                    job["children"].get(s) for s in ("queued", "running", "retrying")
                ):
                    yield b"event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/jobs/{job_id}/retry", tags=["trabajos"])
    def retry_job(job_id: int, _: JobsWriter) -> dict[str, Any]:
        job = ctx.job_repository.requeue(job_id, extra_attempts=1)
        if job is None:
            raise HTTPException(409, "Solo se reintenta un trabajo fallido o cancelado")
        ctx.jobs.publish(job)
        return ctx.job_repository.get(job_id) or job

    @app.post("/api/v1/jobs/{job_id}/cancel", tags=["trabajos"])
    def cancel_job(job_id: int, _: JobsWriter) -> dict[str, Any]:
        _job_or_404(job_id)
        if not ctx.job_repository.cancel(job_id):
            raise HTTPException(409, "El trabajo ya empezó o terminó")
        return {"id": job_id, "status": "cancelled"}

    @app.get("/api/v1/schedules", tags=["trabajos"])
    def list_schedules(_: JobsReader) -> dict[str, Any]:
        return {"items": ctx.job_repository.list_schedules()}

    return app
