from dataclasses import dataclass


@dataclass
class Stock:
    """One listed company from the HK Stock List."""

    stock_code: str  # bare native code, e.g. "00700" — no exchange suffix
    exchange: str  # currently always "HKEX"
    company_name_zh: str
    company_name_en: str
