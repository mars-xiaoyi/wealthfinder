from enum import StrEnum


class ErrorCategory(StrEnum):
    """The part of SAPI an error was raised from. Every raise site sets one."""

    COMMON = "COMMON"
    CONFIG = "CONFIG"
    DB = "DB"
    NEWS_CLIENT = "NEWS_CLIENT"
    REDIS = "REDIS"
    STOCK_LIST = "STOCK_LIST"
