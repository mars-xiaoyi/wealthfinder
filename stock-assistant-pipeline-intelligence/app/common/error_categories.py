from enum import StrEnum


class ErrorCategory(StrEnum):
    """The part of SAPI an error was raised from. Every raise site sets one."""

    AGGREGATION = "AGGREGATION"
    COMMON = "COMMON"
    CONFIG = "CONFIG"
    DB = "DB"
    ENTITY_ANALYSIS = "ENTITY_ANALYSIS"
    LLM = "LLM"
    NEWS_CLIENT = "NEWS_CLIENT"
    REDIS = "REDIS"
    SCORING = "SCORING"
    STOCK_LIST = "STOCK_LIST"
