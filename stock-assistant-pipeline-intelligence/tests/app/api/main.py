from app.api import main as api_main


def create_app():
    app = api_main.create_app()

    routes = {(route.path, method) for route in app.routes for method in route.methods}  # type: ignore[attr-defined]
    assert ("/v1/health", "GET") in routes
    assert ("/v1/admin/hk-stock-list-sync", "POST") in routes
