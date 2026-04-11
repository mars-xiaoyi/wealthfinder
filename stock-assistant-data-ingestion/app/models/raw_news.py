from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID


@dataclass
class RawNews:
    raw_id: UUID                        # Primary key; generate with uuid.uuid4() on insert
    source_name: str                    # e.g. "HKEX", "MINGPAO", "AASTOCKS", "YAHOO_HK"
    source_url: str                     # Original article URL (unique index in DB)
    title: str                          # Original title; never modified after insert
    body: str                           # Plain text body; never modified after insert
    published_at: Optional[datetime]    # UTC; nullable if source doesn't provide it
    created_at: datetime                # UTC; set to now() on insert
    raw_hash: str                       # SHA-256 of normalised title (unique index in DB)
    extra_metadata: Optional[dict]      # e.g. {"stock_code": ["00700", "00388"]} for HKEX (always a list, even for single-issuer rows); null for others
    is_deleted: bool = False            # Soft delete flag set by cleaning layer
    deleted_reason: Optional[str] = None  # "EMPTY_FIELD" / "DUPLICATE_TITLE" / "BODY_TOO_SHORT"
