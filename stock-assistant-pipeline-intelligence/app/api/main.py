from fastapi import FastAPI

from app.api.routes.health import router as health_router

API_VERSION = "v1"


def create_app() -> FastAPI:
    """Create and return the FastAPI application with all routers registered."""
    app = FastAPI(title="SAPI", version="1.0.0")
    app.include_router(health_router, prefix=f"/{API_VERSION}")
    return app
