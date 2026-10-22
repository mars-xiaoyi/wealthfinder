from fastapi import FastAPI

from app.api.error_handlers import register_error_handlers
from app.api.routes.health import router as health_router
from app.api.routes.hk_stock_list_sync import router as hk_stock_list_sync_router

API_VERSION = "v1"


def create_app() -> FastAPI:
    """Create and return the FastAPI application with all routers and error handlers
    registered."""
    app = FastAPI(title="SAPI", version="1.0.0")
    app.include_router(health_router, prefix=f"/{API_VERSION}")
    app.include_router(hk_stock_list_sync_router, prefix=f"/{API_VERSION}")
    register_error_handlers(app)
    return app
