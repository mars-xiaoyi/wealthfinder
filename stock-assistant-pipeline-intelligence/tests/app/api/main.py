from app.api import main as api_main


def create_app():
    app = api_main.create_app()

    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/v1/health" in paths
