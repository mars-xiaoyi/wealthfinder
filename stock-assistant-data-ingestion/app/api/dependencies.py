from fastapi import Request

from app.config import AppConfig
from app.db.connection import DatabaseClient
from app.redis.stream_client import StreamClient


def get_db(request: Request) -> DatabaseClient:
    return request.app.state.db


def get_stream_client(request: Request) -> StreamClient:
    return request.app.state.stream_client


def get_crawl_service(request: Request):
    """Returns the CrawlService instance stored on app.state during lifespan startup."""
    return request.app.state.crawl_service


def get_config(request: Request) -> AppConfig:
    return request.app.state.config
