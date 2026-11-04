import os
import re
import hashlib
import html as html_lib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response

from .config import BASE_DIR
from .database import init_db, get_db
from .security import UserContext, get_current_user, AUTH_MODE
from .image_adapter import load_image_api_config, load_text_api_config
from .task_worker import worker_instance
from .auto_planning_service import planner_worker
from .routers import batches, designs, slots, export, media, system, smart_planning, workbench
from .budget_service import BudgetHardLimitExceededError, BudgetWarningExceededError
from fastapi.requests import Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("workbench")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段：初始化数据库及后台 Worker
    logger.info("Initializing SQLite database with WAL mode...")
    init_db()
    logger.info("Image and text API modes are determined by the configured upstreams")
    await worker_instance.start()
    await planner_worker.start()
    yield
    # 关闭阶段
    logger.info("Stopping background TaskWorker...")
    await worker_instance.stop()
    await planner_worker.stop()

app = FastAPI(
    title="酒店家具智能套图工作台 (Hotel Furniture Workbench)",
    description="用于酒店家具定制视觉方案的智能批量套图与四图终审工作台（第一阶段本地 Mock 闭环）",
    version="1.0.0",
    lifespan=lifespan
)

@app.exception_handler(BudgetWarningExceededError)
async def budget_warning_handler(request: Request, exc: BudgetWarningExceededError):
    return JSONResponse(status_code=423, content={"detail": str(exc)})

@app.exception_handler(BudgetHardLimitExceededError)
async def budget_hard_limit_handler(request: Request, exc: BudgetHardLimitExceededError):
    return JSONResponse(status_code=423, content={"detail": str(exc)})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(batches.router)
app.include_router(designs.router)
app.include_router(slots.router)
app.include_router(export.router)
app.include_router(media.router)
app.include_router(system.router)
app.include_router(smart_planning.router)
app.include_router(workbench.router)

PUBLIC_BASE = os.environ.get("WORKBENCH_PUBLIC_BASE", "").rstrip("/")
STATIC_DIR = BASE_DIR / "static"

@app.middleware("http")
async def static_no_stale_cache(request: Request, call_next):
    """静态资源始终回源校验（304），避免改版后浏览器继续执行旧脚本。"""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

@app.get("/api/session")
def session_identity(user: UserContext = Depends(get_current_user)):
    return {"role": user.role, "user_id": user.user_id, "auth_mode": AUTH_MODE}

@app.get("/api/health")
def health_check():
    # Configuration state only: this endpoint does not contact either upstream.
    with get_db() as conn:
        image_enabled = load_image_api_config(conn)["enabled"]
        text_enabled = load_text_api_config(conn)["enabled"]
    return {
        "status": "online",
        "stage": "Workbench",
        "mock_mode": not image_enabled,
        "network_connected_to_90": None,
        "offline_safe": not image_enabled and not text_enabled,
        "image_api_enabled": image_enabled,
        "text_api_enabled": text_enabled,
    }

# 静态文件与首页
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _STATIC_REF = re.compile(r'((?:src|href)="/static/[^"?]+)')

    def _static_version(name: str) -> str:
        """静态资源内容指纹：文件一改，URL 就变，浏览器不会继续用旧缓存。"""
        try:
            return hashlib.sha1((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        except OSError:
            return "0"

    @app.get("/")
    def read_index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return {"message": "Workbench backend running, static/index.html not found"}
        html = index_file.read_text(encoding="utf-8")

        def _with_version(match: re.Match) -> str:
            name = match.group(1).rsplit("/", 1)[-1]
            return f'{match.group(1)}?v={_static_version(name)}'

        html = _STATIC_REF.sub(_with_version, html)
        if PUBLIC_BASE:
            safe_base = html_lib.escape(PUBLIC_BASE, quote=True)
            html = html.replace('="/static/', f'="{safe_base}/static/')
            html = html.replace('<head>', f'<head><meta name="app-base" content="{safe_base}">', 1)
        return Response(content=html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache, must-revalidate"})
