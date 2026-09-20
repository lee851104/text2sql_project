"""FastAPI application for the PowerQuery TW query pipeline."""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal
from weakref import WeakSet

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT, resolve_configured_paths
from serving.accounts import (
    Account,
    AccountRosterError,
    load_roster,
    resolve_plant_names,
    roster_path,
)
from serving.admin_auth import (
    AdminAuthError,
    AdminAuthManager,
    AdminPrincipal,
    InvalidAdminSession,
    InvalidCredentials,
    LoginRateLimited,
)
from serving.corpus_learning import CorpusLearningService, CorpusSelfApprovalError
from serving.coverage import describe_coverage, load_coverage_document
from serving.data_management import (
    DATA_SLOTS,
    AuditIntegrityError,
    DataManagementConflictError,
    DataManagementError,
    DataManagementNotFoundError,
    DataManagementService,
    DataManagementStateError,
    SeparationOfDutiesError,
)
from serving.presentation import enrich_query_data
from serving.query_log import QueryErrorLog
from serving.raw_data import RawDataError, RawDataNotFoundError, RawDataService
from serving.runtime import RuntimeManager, RuntimeMode, ServiceRuntime

SAFE_RUNTIME_ERRORS = {
    "線上模式尚未設定 API key。",
    "線上模式需要 API key；請在記憶體設定或使用 OPENAI_API_KEY。",
    "請先執行 `uv sync --extra online`。",
}
SWAGGER_UI_VERSION = "5.32.15"
SWAGGER_UI_JS_SRI = "sha384-m7zaGj7MPzU+G4lz2eyy73GxK9bbRDr9bB2CSdj8wodg2wu/Wnt6wsoLP3JD+RS9"
SWAGGER_UI_CSS_SRI = "sha384-fgyWYkUAamzuI8mJFu/xpRP0JWCJRwkwUwsYDoOYVHUJ8NQE5cENn8ib3ppwFFSX"
GENERIC_RUNTIME_ERROR = (
    "線上執行環境初始化失敗；請確認 online 套件、API key 與模型設定。原設定未變更。"
)
GENERIC_CONFIGURATION_ERROR = "執行環境設定無效；請檢查 configs、provider 與模型設定。"
DATA_WORKSPACE_NAME = ".powerquery-data"

ANONYMOUS_SCOPE_ENV = "POWERQUERY_ANONYMOUS_QUERY_SCOPE"
COVERAGE_CONFIG_PATH = PROJECT_ROOT / "configs" / "coverage.yaml"
SELF_APPROVAL_ENV = "POWERQUERY_ALLOW_SELF_APPROVAL"
DEFAULT_ANONYMOUS_SCOPE = "all"
VIEW_SOURCE_SLOTS = {
    "v_unit": ("units_csv",),
    "v_system": ("daily_csv",),
    "v_peak": ("daily_csv", "daily_long_csv", "crosswalk_csv", "units_csv"),
    "v_outage": ("outage_csv", "units_csv"),
    "v_generation_cost": ("generation_cost_csv",),
    "v_re_generation": ("re_sites_csv", "re_generation_csv", "re_sites_supplement_csv"),
}


def _safe_runtime_error(error: Exception) -> str:
    message = str(error)
    return message if message in SAFE_RUNTIME_ERRORS else GENERIC_RUNTIME_ERROR


def _auth_http_error(error: AdminAuthError) -> HTTPException:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if isinstance(error, LoginRateLimited):
        headers["Retry-After"] = str(error.retry_after_seconds)
        return HTTPException(status_code=429, detail=str(error), headers=headers)
    if isinstance(error, (InvalidCredentials, InvalidAdminSession)):
        return HTTPException(status_code=401, detail=str(error), headers=headers)
    return HTTPException(status_code=403, detail=str(error), headers=headers)


def _data_http_error(error: DataManagementError) -> HTTPException:
    if isinstance(error, SeparationOfDutiesError):
        status_code = 403
    elif isinstance(error, DataManagementNotFoundError):
        status_code = 404
    elif isinstance(error, (DataManagementConflictError, DataManagementStateError)):
        status_code = 409
    elif isinstance(error, AuditIntegrityError):
        status_code = 503
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=str(error))


def _build_auth_manager(roster_path: Path) -> tuple[AdminAuthManager, dict[str, Account]]:
    """Prefer the account roster; fall back to the single environment account.

    名冊不存在時維持既有單一管理員行為，方便本機展示；名冊存在但讀不起來時直接失敗，
    不退回單一帳號 —— 權限設定寫錯而服務照常啟動，是最糟的失敗方式。
    """

    if not roster_path.exists():
        return AdminAuthManager.from_environment(), {}
    accounts = load_roster(roster_path)
    return AdminAuthManager.from_roster(accounts), {
        account.username: account for account in accounts
    }


def _self_approval_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Read the single-operator override for the four-eyes rule.

    預設關閉。開啟時仍會把每一筆自審記進變更紀錄與稽核鏈，所以「沒有第二個人看過」
    這件事不會因為設了環境變數就消失。
    """

    source = os.environ if environ is None else environ
    value = source.get(SELF_APPROVAL_ENV, "").strip().casefold()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise AccountRosterError(f"{SELF_APPROVAL_ENV} 必須是 true 或 false。")


def _anonymous_scope_policy(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    value = source.get(ANONYMOUS_SCOPE_ENV, DEFAULT_ANONYMOUS_SCOPE).strip().casefold()
    if value not in {"all", "denied"}:
        raise AccountRosterError(f"{ANONYMOUS_SCOPE_ENV} 必須是 all 或 denied。")
    return value


def _require_admin(request: Request) -> AdminPrincipal:
    try:
        return request.app.state.auth_manager.authenticate_request(request)
    except AdminAuthError as error:
        raise _auth_http_error(error) from error


def _require_admin_mutation(request: Request) -> AdminPrincipal:
    try:
        return request.app.state.auth_manager.authenticate_request(
            request,
            require_csrf=True,
        )
    except AdminAuthError as error:
        raise _auth_http_error(error) from error


def _query_principal(request: Request) -> AdminPrincipal | None:
    """Identify the caller for a read, without widening access when a session lapses.

    沒有帶 cookie 才算匿名。帶了但已過期或被撤銷時回 401，不能悄悄退回匿名範圍 ——
    否則電廠帳號的 session 一過期就會看到全部電廠，權限是往上跳而不是往下掉。
    """

    manager: AdminAuthManager = request.app.state.auth_manager
    token = request.cookies.get(manager.cookie_name)
    if not token:
        return None
    try:
        return manager.authenticate(token)
    except AdminAuthError as error:
        raise _auth_http_error(error) from error


def _require_all_plants(principal: AdminPrincipal) -> AdminPrincipal:
    """Management is an all-plants function; a plant account is a data consumer.

    只收窄「看得到哪些列」並不夠：管理端點不經 `ScopeGuard`，其中
    `/api/data/files/{dataset}` 會直接送出所有電廠的原始來源檔，等於繞過整套授權。
    """

    if principal.plant_id is not None:
        raise HTTPException(
            status_code=403,
            detail="電廠帳號只能查詢自己廠的資料，不能使用管理功能。",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    return principal


def _require_manage_read(request: Request) -> AdminPrincipal:
    return _require_all_plants(_require_admin(request))


def _require_manage_mutation(request: Request) -> AdminPrincipal:
    return _require_all_plants(_require_admin_mutation(request))


def _require_reviewer(request: Request) -> AdminPrincipal:
    """Approving is a capability, not a side effect of being able to manage data.

    沒有這一道的話，任何 `scope: all` 帳號一建立就默默取得發布權 —— 權限預設開啟，
    與這個系統其他地方的 fail-closed 相反。`can_review` 預設 false，要明寫才有。
    """

    principal = _require_manage_mutation(request)
    if not principal.can_review:
        raise HTTPException(
            status_code=403,
            detail="這個帳號沒有審核權；請由名冊上 can_review 的帳號審核。",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    return principal


# Admin* 只驗身分，供登入與登出使用；Manage* 另外要求全廠範圍。
AdminRead = Annotated[AdminPrincipal, Depends(_require_admin)]
AdminMutation = Annotated[AdminPrincipal, Depends(_require_admin_mutation)]
ManageRead = Annotated[AdminPrincipal, Depends(_require_manage_read)]
ManageMutation = Annotated[AdminPrincipal, Depends(_require_manage_mutation)]
ReviewMutation = Annotated[AdminPrincipal, Depends(_require_reviewer)]


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    execution_mode: Literal["offline", "online", "auto"] | None = None
    query_scope: Literal["trusted", "raw", "auto"] = "trusted"


class RuntimeSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["offline", "online", "auto"]
    api_key: SecretStr | None = None
    model: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    ] = None


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    password: SecretStr


class CorpusSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    sql: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    params: Annotated[list[str | int | float], Field(max_length=20)] = []
    intent: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class CorpusReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=500),
    ] = None


class DataUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: Literal[
        "units_csv",
        "daily_csv",
        "crosswalk_csv",
        "daily_long_csv",
        "outage_csv",
        "generation_cost_csv",
    ]
    filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    content_base64: Annotated[str, StringConstraints(min_length=1, max_length=90_000_000)]
    reason: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=500),
    ] = None


class DataRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: Literal[
        "units_csv",
        "daily_csv",
        "crosswalk_csv",
        "daily_long_csv",
        "outage_csv",
        "generation_cost_csv",
    ]
    reason: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=500),
    ] = None


class DataChangeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=500),
    ] = None


class DataRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=500),
    ] = None


def _configured_database(root: Path) -> Path:
    return resolve_configured_paths(root)["database"].resolve()


def create_app(
    runtime: ServiceRuntime | None = None,
    *,
    static_dir: Path | None = None,
    auth_manager: AdminAuthManager | None = None,
    accounts: Sequence[Account] | None = None,
    data_manager: DataManagementService | None = None,
    raw_data_service: RawDataService | None = None,
) -> FastAPI:
    assets = (static_dir or Path(__file__).with_name("static")).resolve()
    application = FastAPI(
        title="PowerQuery TW API",
        version="0.3.0",
        description="台電開放資料的可信任 Text2SQL 查詢介面",
        docs_url=None,
        redoc_url=None,
    )
    application.state.runtime_manager = RuntimeManager(runtime) if runtime is not None else None
    if auth_manager is None:
        application.state.auth_manager, application.state.accounts = _build_auth_manager(
            roster_path()
        )
    else:
        application.state.auth_manager = auth_manager
        application.state.accounts = {account.username: account for account in accounts or ()}
    application.state.anonymous_scope = _anonymous_scope_policy()
    application.state.data_manager = data_manager
    application.state.data_base_directory = (
        runtime.database.parent
        if runtime is not None
        else _configured_database(PROJECT_ROOT).parent
    )
    application.state.learning_workspace = (
        application.state.data_base_directory / ".powerquery-learning"
    )
    application.state.learning_service = None
    diagnostic_workspace = (
        data_manager.workspace
        if data_manager is not None
        else application.state.data_base_directory / DATA_WORKSPACE_NAME
    )
    application.state.raw_data_service = raw_data_service or RawDataService(
        root=PROJECT_ROOT,
        database=application.state.data_base_directory / "raw_open_data.db",
    )
    application.state.query_error_log = QueryErrorLog(diagnostic_workspace / "query-errors.jsonl")
    application.state.learning_pipelines = WeakSet()
    application.state.initialization_lock = RLock()

    @application.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del request
        # FastAPI's default 422 payload echoes invalid input. Omitting that field
        # prevents malformed API-key or question bodies from being reflected.
        details = []
        for item in error.errors():
            sanitized = dict(item)
            sanitized.pop("input", None)
            details.append(sanitized)
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": details}))

    def switch_managed_runtime(database: Path) -> ServiceRuntime | None:
        """Switch this worker after a reviewed data publication."""

        manager = application.state.runtime_manager
        if manager is None:
            # Startup recovery runs before this worker has a runtime.  The manager
            # is built from the recovered, fully verified snapshot immediately
            # after the data service returns.
            return None
        if not isinstance(manager, RuntimeManager):
            raise RuntimeError("執行環境不支援資料版本切換。")
        return manager.switch_database(database)

    def build_managed_service(
        *,
        root: Path,
        initial_database: Path,
        workspace: Path,
    ) -> DataManagementService:
        configured = resolve_configured_paths(root)
        sources = {name: configured[name] for name in DATA_SLOTS}

        def build_candidate(target: Path, source_paths: dict[str, Path]):
            return build_database(
                target,
                root=root,
                source_paths=source_paths,
            )

        return DataManagementService(
            workspace=workspace,
            source_paths=sources,
            initial_database=initial_database,
            build_database=build_candidate,
            switch_runtime=switch_managed_runtime,
            allow_self_approval=_self_approval_allowed(),
        )

    def current_manager() -> RuntimeManager:
        if application.state.runtime_manager is None:
            with application.state.initialization_lock:
                if application.state.runtime_manager is None:
                    try:
                        # Construct and validate the data-management workspace first.
                        # This ensures a restarted worker never opens an active DB
                        # before its pointer, version record, sources and DB checksum
                        # have passed integrity validation.
                        canonical = _configured_database(PROJECT_ROOT)
                        managed = application.state.data_manager
                        if managed is None:
                            managed = build_managed_service(
                                root=PROJECT_ROOT,
                                initial_database=canonical,
                                workspace=canonical.parent / DATA_WORKSPACE_NAME,
                            )
                        snapshot = managed.active_snapshot()
                        application.state.runtime_manager = RuntimeManager(
                            root=PROJECT_ROOT,
                            database=Path(snapshot["database"]),
                        )
                        application.state.data_manager = managed
                    except (DataManagementError, FileNotFoundError, OSError) as error:
                        raise HTTPException(
                            status_code=503,
                            detail="資料庫尚未就緒，或作用中資料版本未通過完整性驗證。",
                        ) from error
                    except (TypeError, ValueError) as error:
                        raise HTTPException(
                            status_code=503,
                            detail=GENERIC_CONFIGURATION_ERROR,
                        ) from error
                    except RuntimeError as error:
                        raise HTTPException(
                            status_code=503,
                            detail=_safe_runtime_error(error),
                        ) from error
        return application.state.runtime_manager

    def current_data_manager() -> DataManagementService:
        with application.state.initialization_lock:
            managed = application.state.data_manager
            if managed is not None:
                return managed
            manager = current_manager()
            service = manager.active_runtime
            workspace = application.state.data_base_directory / DATA_WORKSPACE_NAME

            try:
                managed = build_managed_service(
                    root=service.root,
                    initial_database=service.database,
                    workspace=workspace,
                )
                snapshot = managed.active_snapshot()
                manager.get_runtime_for_database(Path(snapshot["database"]))
            except (DataManagementError, FileNotFoundError, OSError, ValueError) as error:
                raise HTTPException(status_code=503, detail="資料管理工作區尚未就緒。") from error
            application.state.data_manager = managed
            return managed

    def required_data_manager() -> DataManagementService:
        return current_data_manager()

    def data_provenance(
        tables: list[str] | tuple[str, ...],
        snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        """Describe sources from the same immutable snapshot used by the query."""

        version = str(snapshot["version"])
        files = snapshot["sources"]
        if not isinstance(files, Mapping):
            raise ValueError("作用中資料來源 snapshot 無效。")
        selected_slots: set[str] = set()
        for table in tables:
            selected_slots.update(VIEW_SOURCE_SLOTS.get(table, ()))
        sources = []
        for slot in sorted(selected_slots):
            source = files[slot]
            if not isinstance(source, Mapping):
                raise ValueError("作用中資料來源 snapshot 無效。")
            source_views = [
                view
                for view, slots in VIEW_SOURCE_SLOTS.items()
                if slot in slots and view in tables
            ]
            sources.append(
                {
                    "file_id": f"{version}:{slot}",
                    "dataset": slot,
                    "display_name": source["filename"],
                    "version": version,
                    "sha256": source.get("sha256"),
                    "bytes": source.get("bytes", 0),
                    "present": source["present"],
                    "views": source_views,
                }
            )
        return {
            "database_version": version,
            "database_sha256": snapshot["database_sha256"],
            "data_sources": sources,
        }

    def current_runtime_snapshot(
        mode: RuntimeMode | None = None,
    ) -> tuple[ServiceRuntime, Mapping[str, object] | None]:
        """Return a request-pinned runtime and its verified data snapshot.

        Every real worker reads the shared atomic pointer on every request.  A
        publication made by another worker therefore causes this worker to build
        and atomically select the new runtime before serving the request.
        """

        manager = current_manager()
        # Lightweight test/embed managers do not participate in managed hot-swap.
        if not isinstance(manager, RuntimeManager):
            return manager.get_runtime(mode), None
        try:
            snapshot = current_data_manager().active_snapshot()
            service = manager.get_runtime_for_database(Path(snapshot["database"]), mode)
            if service.database != Path(snapshot["database"]).resolve():
                raise RuntimeError("查詢執行環境與資料 snapshot 不一致。")
            return service, snapshot
        except (DataManagementError, FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise HTTPException(
                status_code=503,
                detail="作用中資料版本未通過完整性驗證。",
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=_safe_runtime_error(error)) from error

    def current_runtime(mode: RuntimeMode | None = None) -> ServiceRuntime:
        return current_runtime_snapshot(mode)[0]

    def current_learning(service: ServiceRuntime) -> CorpusLearningService:
        with application.state.initialization_lock:
            learning = application.state.learning_service
            learning_database = getattr(learning, "database", None)
            service_database = getattr(service, "database", None)
            if learning is None or (
                learning_database is not None
                and service_database is not None
                and learning_database != service_database
            ):
                learning = CorpusLearningService(
                    database=service.database,
                    canonical_corpus_path=service.root / "corpus/training_corpus.json",
                    benchmark_dir=service.root / "benchmarks",
                    pipeline=service.pipeline,
                    workspace=application.state.learning_workspace,
                    allow_self_approval=_self_approval_allowed(),
                )
                application.state.learning_service = learning
                application.state.learning_pipelines = WeakSet({service.pipeline})
            elif service.pipeline not in application.state.learning_pipelines:
                learning.attach_pipeline(service.pipeline)
                application.state.learning_pipelines.add(service.pipeline)
            return learning

    def required_learning(service: ServiceRuntime) -> CorpusLearningService:
        try:
            return current_learning(service)
        except (FileNotFoundError, OSError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail="語料學習工作區尚未就緒。") from error

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://cdn.plot.ly; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'"
            )
        protected_path = request.url.path
        if (
            protected_path.startswith("/api/admin/")
            or protected_path.startswith("/api/runtime/")
            or protected_path.startswith("/api/corpus/")
            or protected_path.startswith("/api/data/")
            or protected_path == "/api/training-status"
        ):
            application.state.auth_manager.mark_no_store(response)
        return response

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(assets / "index.html")

    @application.get("/docs", include_in_schema=False)
    def api_docs() -> HTMLResponse:
        nonce = secrets.token_urlsafe(24)
        swagger_js_url = (
            "https://cdn.jsdelivr.net/npm/"
            f"swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js"
        )
        swagger_css_url = (
            f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css"
        )
        page = get_swagger_ui_html(
            openapi_url=application.openapi_url,
            title=f"{application.title} - Swagger UI",
            swagger_js_url=swagger_js_url,
            swagger_css_url=swagger_css_url,
            swagger_favicon_url="/static/favicon.svg",
        )
        body = page.body.decode("utf-8")
        body = body.replace(
            f'<link type="text/css" rel="stylesheet" href="{swagger_css_url}">',
            '<link type="text/css" rel="stylesheet" '
            f'href="{swagger_css_url}" integrity="{SWAGGER_UI_CSS_SRI}" '
            'crossorigin="anonymous">',
        )
        body = body.replace(
            f'<script src="{swagger_js_url}"></script>',
            f'<script src="{swagger_js_url}" integrity="{SWAGGER_UI_JS_SRI}" '
            'crossorigin="anonymous"></script>',
        )
        body = body.replace("<script", f'<script nonce="{nonce}"')
        docs_csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            "style-src https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return HTMLResponse(body, headers={"Content-Security-Policy": docs_csp})

    def anonymous_admin_status() -> dict[str, object]:
        policy = application.state.auth_manager.public_configuration()
        return {
            "authenticated": False,
            "using_default_credentials": policy["using_default_credentials"],
            "session_ttl_seconds": policy["session_ttl_seconds"],
        }

    @application.post("/api/admin/session")
    def create_admin_session(
        payload: AdminLoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        manager: AdminAuthManager = application.state.auth_manager
        try:
            manager.validate_login_request(request)
            # 限速要分到真正的來源；走信任代理時 request.client 會是代理自己。
            client_id = manager.client_address(request)
            grant = manager.login(
                payload.username,
                payload.password.get_secret_value(),
                client_id=client_id,
            )
        except AdminAuthError as error:
            raise _auth_http_error(error) from error
        manager.set_session_cookie(
            response,
            grant,
            secure=manager.cookie_should_be_secure(request),
        )
        return {
            "success": True,
            "data": {
                **grant.principal.to_public_dict(),
                "csrf_token": grant.csrf_token,
                "session_ttl_seconds": manager.session_ttl_seconds,
            },
        }

    @application.get("/api/admin/session")
    def admin_session_status(request: Request, response: Response) -> dict[str, object]:
        manager: AdminAuthManager = application.state.auth_manager
        token = request.cookies.get(manager.cookie_name)
        if not token:
            return {"success": True, "data": anonymous_admin_status()}
        try:
            principal, csrf_token = manager.issue_csrf(token)
        except InvalidAdminSession:
            manager.clear_session_cookie(
                response,
                secure=manager.cookie_should_be_secure(request),
            )
            return {"success": True, "data": anonymous_admin_status()}
        return {
            "success": True,
            "data": {
                **principal.to_public_dict(),
                "csrf_token": csrf_token,
                "session_ttl_seconds": manager.session_ttl_seconds,
            },
        }

    @application.delete("/api/admin/session")
    def delete_admin_session(
        request: Request,
        response: Response,
        _principal: AdminMutation,
    ) -> dict[str, object]:
        manager: AdminAuthManager = application.state.auth_manager
        manager.revoke_request(request)
        manager.clear_session_cookie(
            response,
            secure=manager.cookie_should_be_secure(request),
        )
        return {"success": True, "data": anonymous_admin_status()}

    @application.get("/api/health")
    def health() -> dict[str, object]:
        try:
            service = current_runtime()
            mode = current_manager().status()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=_safe_runtime_error(error)) from error
        return {
            "success": True,
            "demo": not service.online_llm,
            "online_llm": service.online_llm,
            "mode": mode,
            "data_range": {"start": service.data_range[0], "end": service.data_range[1]},
        }

    @application.get("/api/stats")
    def stats() -> dict[str, object]:
        service = current_runtime()

        def scalar(sql: str) -> int:
            _columns, rows = service.executor.execute(sql, ())
            return int(rows[0][0])

        return {
            "success": True,
            "data": {
                "total_records": scalar("SELECT COUNT(*) FROM v_peak LIMIT 1"),
                "total_units": scalar("SELECT COUNT(*) FROM v_unit LIMIT 1"),
                "outage_records": scalar("SELECT COUNT(*) FROM v_outage LIMIT 1"),
                "date_range": f"{service.data_range[0]} ~ {service.data_range[1]}",
            },
        }

    @application.get("/api/training-status")
    def training_status(
        _principal: ManageRead,
    ) -> dict[str, object]:
        status = required_learning(current_runtime()).status()
        ready = bool(status["workspace_ready"] and status["index_synchronized"])
        return {
            "success": True,
            "is_training_complete": ready,
            "training_status": "語料學習工作區已就緒" if ready else "語料索引待同步",
            "data": status,
        }

    @application.get("/api/runtime/llm")
    def runtime_status(
        _principal: ManageRead,
    ) -> dict[str, object]:
        return {"success": True, "data": current_manager().status()}

    @application.put("/api/runtime/llm")
    def configure_runtime(
        payload: RuntimeSettingsRequest,
        principal: ManageMutation,
    ) -> dict[str, object]:
        manager = current_manager()
        before = manager.status()
        updates: dict[str, object] = {"mode": payload.mode}
        if "model" in payload.model_fields_set and payload.model is not None:
            updates["model"] = payload.model
        if "api_key" in payload.model_fields_set:
            updates["api_key"] = (
                payload.api_key.get_secret_value() if payload.api_key is not None else None
            )
        try:
            status = manager.configure_and_status(**updates)
        except (RuntimeError, TypeError, ValueError) as error:
            with suppress(DataManagementError, HTTPException, OSError, ValueError, AttributeError):
                required_data_manager().record_audit(
                    "runtime_configuration_failed",
                    actor=principal.username,
                    details={
                        "requested_mode": payload.mode,
                        "requested_model": payload.model,
                        "api_key_supplied": "api_key" in payload.model_fields_set,
                        "error_type": type(error).__name__,
                    },
                )
            raise HTTPException(status_code=400, detail=_safe_runtime_error(error)) from error
        with suppress(DataManagementError, HTTPException, OSError, ValueError, AttributeError):
            required_data_manager().record_audit(
                "runtime_configuration_changed",
                actor=principal.username,
                details={
                    "before": before,
                    "after": status,
                    "api_key_changed": "api_key" in payload.model_fields_set,
                },
            )
            # Runtime configuration has its own memory-only boundary; a data audit
            # workspace may be unavailable in embedded/test deployments.
        return {"success": True, "data": status}

    @application.get("/api/corpus/entries")
    def corpus_entries(
        _principal: ManageRead,
        state: Literal[
            "all", "validating", "pending_review", "promoted", "rejected", "ignored"
        ] = "all",
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        learning = required_learning(current_runtime())
        entries = learning.list_entries(state=None if state == "all" else state, limit=limit)
        return {
            "success": True,
            "data": {
                "state": state,
                "entries": entries,
                "returned": len(entries),
            },
        }

    @application.get("/api/corpus/events")
    def corpus_events(
        _principal: ManageRead,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        events = required_learning(current_runtime()).list_events(limit=limit)
        return {"success": True, "data": {"events": events, "returned": len(events)}}

    @application.post("/api/corpus/entries")
    def submit_corpus_entry(
        payload: CorpusSubmissionRequest,
        principal: ManageMutation,
    ) -> dict[str, object]:
        """Accept a hand-written example, subject to the same gates as a learned one.

        手動提供不是繞過驗證的後門：SQL 先過安全守門，再實際執行取得真實結果，之後與
        自動抓取的候選走同一條審核路徑（洩漏、去重、SQL 守門、問句語意、SQL 語意、
        重跑結果比對、檢索回歸）。放寬的只有「誰可以核准」，不是「內容要不要驗」。
        """

        service = current_runtime()
        learning = required_learning(service)
        params = tuple(payload.params)

        decision = service.pipeline.sql_guard.validate(payload.sql, params)
        if not decision.allowed:
            raise HTTPException(
                status_code=400,
                detail=f"SQL 未通過安全守門（{decision.code}）：{decision.reason}",
            )
        try:
            columns, rows = service.pipeline.run_sql(payload.sql, params)
        except Exception as error:
            raise HTTPException(
                status_code=400,
                detail=f"SQL 無法執行，因此無法確認結果：{type(error).__name__}",
            ) from error
        if not rows:
            # 空結果無法證明這組問答是對的，而且審核時的重跑比對會拿它跟空結果比，
            # 等於沒有檢查。寧可在這裡擋下來說明原因。
            raise HTTPException(
                status_code=400,
                detail="這組 SQL 查不到任何資料，無法作為語料範例。",
            )

        try:
            entry = learning.submit(
                question=payload.question,
                sql=payload.sql,
                params=params,
                intent=payload.intent,
                source="manual",
                columns=columns,
                rows=rows,
                tables=decision.tables,
                pipeline=service.pipeline,
                proposed_by=principal.username,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        if entry.get("status") == "rejected":
            # submit 會立刻跑一次關卡。被當場駁回時回 200 會讓前端顯示「已送出」，
            # 使用者要再去候選清單才發現它已經死了。直接說明原因。
            raise HTTPException(
                status_code=400,
                detail=f"候選未通過驗證關卡：{entry.get('reason')}",
            )

        with suppress(DataManagementError, HTTPException, OSError, ValueError, AttributeError):
            required_data_manager().record_audit(
                "corpus_candidate_submitted",
                actor=principal.username,
                details={"candidate_id": entry.get("id"), "source": "manual"},
            )
        return {"success": True, "data": entry}

    @application.post("/api/corpus/entries/{candidate_id}/review")
    def review_corpus_entry(
        candidate_id: str,
        payload: CorpusReviewRequest,
        principal: ReviewMutation,
    ) -> dict[str, object]:
        service = current_runtime()
        learning = required_learning(service)
        try:
            entry = learning.review(
                candidate_id,
                approve=payload.decision == "approve",
                reviewer=principal.username,
                note=payload.note or "",
                pipeline=service.pipeline,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到指定的語料候選。") from error
        except CorpusSelfApprovalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=409,
                detail="候選狀態已變更，或目前不可審核。",
            ) from error
        with suppress(DataManagementError, HTTPException, OSError, ValueError, AttributeError):
            required_data_manager().record_audit(
                "corpus_candidate_reviewed",
                actor=principal.username,
                details={
                    "candidate_id": candidate_id,
                    "decision": payload.decision,
                    "resulting_status": entry.get("status"),
                },
            )
            # CorpusLearningService already wrote its own durable review event.
        return {"success": True, "data": entry}

    @application.get("/api/data/status")
    def data_status(_principal: ManageRead) -> dict[str, object]:
        try:
            return {"success": True, "data": required_data_manager().status()}
        except DataManagementError as error:
            raise _data_http_error(error) from error

    @application.get("/api/data/files")
    def data_files(_principal: ManageRead) -> dict[str, object]:
        try:
            return {"success": True, "data": required_data_manager().list_sources()}
        except DataManagementError as error:
            raise _data_http_error(error) from error

    @application.get("/api/data/files/{dataset}")
    def download_data_file(
        dataset: str,
        _principal: ManageRead,
        version: Annotated[
            str | None,
            Query(min_length=69, max_length=69),
        ] = None,
    ) -> FileResponse:
        try:
            managed = required_data_manager()
            path = managed.source_file(dataset, version=version)
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return FileResponse(
            path,
            media_type="text/csv; charset=utf-8",
            filename=DATA_SLOTS[dataset].filename,
        )

    @application.get("/api/data/query-errors")
    def download_query_errors(_principal: ManageRead) -> FileResponse:
        path = application.state.query_error_log.path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="目前沒有查詢錯誤紀錄。")
        return FileResponse(
            path,
            media_type="application/x-ndjson; charset=utf-8",
            filename="powerquery-query-errors.jsonl",
        )

    @application.get("/api/data/changes")
    def data_changes(
        _principal: ManageRead,
        state: Literal["all", "pending_review", "approved", "rejected", "failed"] = "all",
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        try:
            changes = required_data_manager().list_changes(
                state=None if state == "all" else state,
                limit=limit,
            )
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return {
            "success": True,
            "data": {"state": state, "changes": changes, "returned": len(changes)},
        }

    @application.get("/api/data/changes/{change_id}")
    def data_change(change_id: str, _principal: ManageRead) -> dict[str, object]:
        try:
            return {"success": True, "data": required_data_manager().get_change(change_id)}
        except DataManagementError as error:
            raise _data_http_error(error) from error

    @application.get("/api/data/versions")
    def data_versions(
        _principal: ManageRead,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        try:
            versions = required_data_manager().list_versions(limit=limit)
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return {"success": True, "data": {"versions": versions, "returned": len(versions)}}

    @application.get("/api/data/events")
    def data_events(
        _principal: ManageRead,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        try:
            events = required_data_manager().list_audit(limit=limit)
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return {"success": True, "data": {"events": events, "returned": len(events)}}

    @application.post("/api/data/changes/upload")
    def stage_data_upload(
        payload: DataUploadRequest,
        principal: ManageMutation,
    ) -> dict[str, object]:
        try:
            change = required_data_manager().stage_upload(
                payload.dataset,
                payload.content_base64,
                actor=principal.username,
                filename=payload.filename,
                reason=payload.reason or "",
            )
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return {"success": True, "data": change}

    @application.post("/api/data/changes/remove")
    def stage_data_remove(
        payload: DataRemoveRequest,
        principal: ManageMutation,
    ) -> dict[str, object]:
        try:
            change = required_data_manager().stage_remove(
                payload.dataset,
                actor=principal.username,
                reason=payload.reason or "",
            )
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return {"success": True, "data": change}

    @application.post("/api/data/changes/{change_id}/review")
    def review_data_change(
        change_id: str,
        payload: DataChangeReviewRequest,
        principal: ReviewMutation,
    ) -> dict[str, object]:
        try:
            change = required_data_manager().review(
                change_id,
                approve=payload.decision == "approve",
                reviewer=principal.username,
                reason=payload.note or "",
            )
        except DataManagementError as error:
            raise _data_http_error(error) from error
        if change.get("status") == "approved":
            with application.state.initialization_lock:
                application.state.learning_service = None
                application.state.learning_pipelines = WeakSet()
        return {"success": True, "data": change}

    @application.post("/api/data/versions/{version}/rollback")
    def stage_data_rollback(
        version: str,
        payload: DataRollbackRequest,
        principal: ManageMutation,
    ) -> dict[str, object]:
        try:
            change = required_data_manager().stage_rollback(
                version,
                actor=principal.username,
                reason=payload.reason or "",
            )
        except DataManagementError as error:
            raise _data_http_error(error) from error
        return {"success": True, "data": change}

    @application.get("/api/coverage")
    def coverage() -> dict[str, object]:
        """Describe what the active database covers, and what it cannot answer.

        數量與範圍一律現查，跟著作用中的資料庫走；資料換版後說明自動跟著變。只有
        「每個檢視回答什麼」與「答不出什麼」來自設定檔，而每條限制都附可驗證的條件。
        """

        service = current_runtime()
        views, limitations = load_coverage_document(COVERAGE_CONFIG_PATH)
        try:
            data = describe_coverage(service.database, views=views, limitations=limitations)
        except sqlite3.Error as error:
            raise HTTPException(status_code=503, detail="目前無法讀取資料涵蓋範圍。") from error
        return {"success": True, "data": data}

    @application.get("/api/examples")
    def examples() -> dict[str, object]:
        return {
            "success": True,
            "data": [
                # 前三題是入門問句：先讓人摸到資料的邊界，再問細節。
                "有哪些電廠",
                "資料涵蓋到什麼時候",
                "有哪些燃料別",
                "2026年7月備轉容量率最低是哪一天？",
                "2026年7月20日出力前五名機組",
                "列出台中發電廠所有設備",
                "天然氣機組共有幾台？",
                "2026年三月有哪些機組在歲修？",
            ],
        }

    # 原始檔端點給的是整份來源檔，不經 ScopeGuard —— 和 `/api/data/files/{dataset}` 同一類，
    # 所以套同一道 `ManageRead`（要登入，且必須是全廠帳號）。
    #
    # 先前這三個讀取端點完全沒有權限，於是 `/api/query` 那句「電廠帳號不能查原始開放資料檔」
    # 的 403 擋不住任何人：同一個人換個網址就拿到同樣的東西，連登入都不用。
    # 寫入端 `/api/raw/rebuild` 本來就要 `ManageMutation`，只有讀取這邊漏掉。
    @application.get("/api/raw/status")
    def raw_status(_principal: ManageRead) -> dict[str, object]:
        try:
            return {"success": True, "data": application.state.raw_data_service.status()}
        except RawDataError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/raw/resources")
    def raw_resources(
        _principal: ManageRead,
        search: str | None = Query(default=None, max_length=200),
        format_name: str | None = Query(default=None, alias="format", max_length=10),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        try:
            resources = application.state.raw_data_service.list_resources(
                search=search,
                format_name=format_name,
                limit=limit,
            )
        except RawDataError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"success": True, "data": {"resources": resources, "count": len(resources)}}

    @application.get("/api/raw/resources/{resource_id}/rows")
    def raw_resource_rows(
        resource_id: str,
        _principal: ManageRead,
        search: str | None = Query(default=None, max_length=200),
        member: str | None = Query(default=None, max_length=500),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        try:
            data = application.state.raw_data_service.read_rows(
                resource_id,
                search=search,
                member=member,
                limit=limit,
                offset=offset,
            )
        except RawDataNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RawDataError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"success": True, "data": data}

    @application.post("/api/raw/rebuild")
    def rebuild_raw_catalog(_principal: ManageMutation) -> dict[str, object]:
        try:
            return {"success": True, "data": application.state.raw_data_service.rebuild()}
        except RawDataError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    def resolve_account_plant(
        principal: AdminPrincipal | None, service: ServiceRuntime
    ) -> str | None:
        """Resolve the stable plant id on a session into today's plant name."""

        if principal is None or principal.plant_id is None:
            return None
        guard = getattr(service.pipeline, "scope_guard", None)
        if guard is None:
            raise HTTPException(
                status_code=503,
                detail="此服務未載入授權對照，無法提供電廠帳號查詢。",
            )
        account = application.state.accounts.get(principal.username)
        if account is None:
            raise HTTPException(status_code=503, detail="帳號名冊與目前的登入階段不一致。")
        try:
            resolved = resolve_plant_names([account], guard.catalog.plant_names_by_id())
        except AccountRosterError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return resolved[principal.username]

    @application.post("/api/query")
    def query(
        payload: QueryRequest,
        principal: Annotated[AdminPrincipal | None, Depends(_query_principal)],
    ) -> dict[str, object]:
        plant_account = principal is not None and principal.plant_id is not None
        if principal is None and application.state.anonymous_scope == "denied":
            raise HTTPException(status_code=401, detail="此服務的查詢需要先登入。")
        if plant_account and payload.query_scope != "trusted":
            # 原始檔查詢不經 ScopeGuard；開放給電廠帳號等於留一條繞過授權的路。
            raise HTTPException(
                status_code=403,
                detail="電廠帳號只能查詢受授權管制的語意檢視，不能查詢原始開放資料檔。",
            )
        # 語意檢視經過 SqlGuard 與 ScopeGuard，原始檔兩道都不經過，給的是整份來源檔，
        # 所以它比 trusted 查詢更嚴：不看 anonymous_scope，一律要登入且是全廠帳號，
        # 與 `/api/raw/*` 的 ManageRead 對齊。少了這道，匿名訪客改個 query_scope 就繞過去。
        raw_allowed = principal is not None and not plant_account
        if payload.query_scope == "raw" and not raw_allowed:
            raise HTTPException(status_code=401, detail="原始開放資料檔的查詢需要先登入。")
        if payload.query_scope == "raw":
            try:
                return application.state.raw_data_service.query(payload.question)
            except RawDataNotFoundError as error:
                response = {
                    "success": False,
                    "error": str(error),
                    "error_code": "RAW_RESOURCE_NOT_FOUND",
                    "severity": "error",
                }
            except RawDataError as error:
                response = {
                    "success": False,
                    "error": str(error),
                    "error_code": "RAW_QUERY_FAILED",
                    "severity": "error",
                }
            response["diagnostic_id"] = application.state.query_error_log.record_failure(
                question=payload.question,
                requested_mode=None,
                requested_scope="raw",
                runtime={"mode": "raw", "provider": "local-files", "model": None},
                response=response,
            )
            return response
        service, data_snapshot = current_runtime_snapshot(payload.execution_mode)
        learning: CorpusLearningService | None
        try:
            learning = current_learning(service)
        except (FileNotFoundError, OSError, ValueError, RuntimeError):
            learning = None
        plant = resolve_account_plant(principal, service)
        pipeline_response = service.pipeline.query(payload.question, plant=plant)
        response = pipeline_response.to_dict()
        if response.get("error_code") == "SCOPE_DENIED":
            with suppress(DataManagementError, HTTPException, OSError, ValueError, AttributeError):
                required_data_manager().record_audit(
                    "scope_denied",
                    actor=principal.username if principal is not None else "anonymous",
                    details={"plant": plant, "error": response.get("error")},
                )
        # auto 的語意是「盡量答」，所以沒有原始檔權限時安靜地不退回，而不是回 401 ——
        # 匿名訪客該拿到 trusted 的結果或它的失敗訊息，不該因為選了 auto 就被要求登入。
        if not response["success"] and payload.query_scope == "auto" and raw_allowed:
            try:
                fallback = application.state.raw_data_service.query(payload.question)
            except RawDataError:
                fallback = None
            fallback_data = fallback.get("data") if isinstance(fallback, Mapping) else None
            if (
                isinstance(fallback, Mapping)
                and fallback.get("success") is True
                and isinstance(fallback_data, dict)
                and fallback_data.get("intent") == "raw_resource_lookup"
            ):
                fallback_data["fallback_from"] = "trusted"
                fallback_data["trusted_error_code"] = response.get("error_code")
                return dict(fallback)
        if response["success"] and isinstance(response.get("data"), dict):
            tables = [str(table) for table in response["data"].get("tables", ())]
            try:
                if data_snapshot is None:
                    raise ValueError("data snapshot unavailable")
                provenance = data_provenance(tables, data_snapshot)
            except (DataManagementError, HTTPException, OSError, ValueError):
                provenance = {
                    "database_version": (
                        str(data_snapshot["version"])
                        if data_snapshot is not None
                        else (
                            learning.data_manifest_version
                            if learning is not None
                            else service.database.name
                        )
                    ),
                    "database_sha256": (
                        data_snapshot.get("database_sha256") if data_snapshot is not None else None
                    ),
                    "data_sources": [],
                }
            learning_result = (
                learning.observe(
                    pipeline_response,
                    pipeline=service.pipeline,
                    data_provenance=provenance,
                    proposed_by=principal.username if principal is not None else None,
                )
                if learning is not None
                else {
                    "accepted": False,
                    "status": "error",
                    "reason": "learning_workspace_unavailable",
                }
            )
            response["data"] = enrich_query_data(response["data"])
            response["data"]["query_scope"] = "trusted"
            if plant is not None and not response["data"].get("rows"):
                # 空結果對電廠帳號是有歧義的：可能真的沒有，也可能是被授權範圍擋掉。
                # 底層是公開資料，藏起邊界沒有保護作用，只會讓人一直重問，所以講明。
                response["data"]["scope_notice"] = (
                    f"此帳號的資料範圍只涵蓋{plant}與跨廠共用欄位。"
                    "查無資料可能是超出授權範圍，不代表該筆資料不存在。"
                )
            response["data"]["runtime"] = {
                "mode": service.mode,
                "provider": service.provider,
                "model": service.model,
            }
            response["data"]["data_provenance"] = provenance
            response["data"]["learning"] = learning_result
        if not response["success"]:
            response["diagnostic_id"] = application.state.query_error_log.record_failure(
                question=payload.question,
                requested_mode=payload.execution_mode,
                requested_scope=payload.query_scope,
                runtime={
                    "mode": service.mode,
                    "provider": service.provider,
                    "model": service.model,
                    "database": getattr(getattr(service, "database", None), "name", None),
                },
                response=response,
            )
        return response

    application.mount("/static", StaticFiles(directory=assets), name="static")
    return application


app = create_app()
