import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .config import BASE_DIR, MOCK_MODE, ALLOW_REMOTE_NETWORK
from .database import init_db
from .task_worker import worker_instance
from .routers import batches, designs, slots, export, media, system, smart_planning
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
    logger.info(f"System operating in MOCK_MODE={MOCK_MODE}, ALLOW_REMOTE_NETWORK={ALLOW_REMOTE_NETWORK}")
    await worker_instance.start()
    yield
    # 关闭阶段
    logger.info("Stopping background TaskWorker...")
    await worker_instance.stop()

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

STATIC_DIR = BASE_DIR / "static"

@app.get("/api/health")
def health_check():
    return {
        "status": "online",
        "stage": "Phase 1 - Local Mock Complete Loop",
        "mock_mode": MOCK_MODE,
        "network_connected_to_90": False,
        "offline_safe": True
    }

# 静态文件与首页
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def read_index():
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"message": "Workbench backend running, static/index.html not found"}
