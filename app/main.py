import asyncio
import os
import importlib
from contextlib import asynccontextmanager
from app.task_config import start
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import settings, init_db, close_db
from app.redis import init_redis, redis_client
from app.routes import register_routes
from app.utils.sync_permissions import seed_feature_permissions as sync_permissions
from app.utils.auto_routing import get_module
from app.dummy.users import create_test_users

# import logging
# logging.basicConfig(level=logging.DEBUG)

_runtime_init_lock = asyncio.Lock()
_core_services_ready = False
_startup_tasks_ran = False


async def _initialize_core_services_unlocked() -> None:
    global _core_services_ready

    if _core_services_ready:
        return

    await init_db()
    init_redis()
    _core_services_ready = True


async def ensure_core_services() -> None:
    if _core_services_ready:
        return

    async with _runtime_init_lock:
        await _initialize_core_services_unlocked()


async def run_startup_tasks() -> None:
    global _startup_tasks_ran

    if _startup_tasks_ran:
        return

    async with _runtime_init_lock:
        if _startup_tasks_ran:
            return

        await _initialize_core_services_unlocked()
        start()
        await sync_permissions()

        for app_name in get_module(base_dir="applications"):
            try:
                importlib.import_module(f"applications.{app_name}.signals")
            except ModuleNotFoundError:
                print(f"[startup] warning: no signals.py in '{app_name}' sub-app.")

        _startup_tasks_ran = True

@asynccontextmanager
async def lifespan(routerAPI: FastAPI):
    await run_startup_tasks()

    if settings.CREATE_DUMMY_DATA:
        try:
            await create_test_users()
        except Exception as error:
            print(f"startup seeding failed: {error}")
    yield
    if redis_client:
        await redis_client.aclose()
    await close_db()
    print("Application shutdown complete.")

app = FastAPI(
    lifespan=lifespan,
    debug=settings.DEBUG,
    swagger_ui_parameters={"persistAuthorization": True},
)


@app.middleware("http")
async def ensure_runtime_dependencies(request: Request, call_next):
    try:
        await ensure_core_services()
    except Exception as error:
        print(f"[startup] runtime initialization failed: {error}", flush=True)
        return JSONResponse(
            status_code=503,
            content={"detail": "Application database is not initialized."},
        )
    return await call_next(request)


register_routes(app)

# if settings.DEBUG else "index.html"

templates = Jinja2Templates(directory="templates")
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    routes = get_module()
    html_file = "development.html"
    return templates.TemplateResponse(
        html_file,
        {
            "request": request, 
            "routes": routes,
            "image_url": "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1920&q=80"
        }
    )


ALLOWED_HOST = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173", "http://sc5jema6006.universe.wf"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


os.makedirs(settings.MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="templates/static"), name="static")
app.mount("/media", StaticFiles(directory=settings.MEDIA_DIR), name="media")
