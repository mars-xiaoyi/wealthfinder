from app.db.connection import DatabaseClient


async def is_duplicate(raw_hash: str, db: DatabaseClient) -> bool:
    """
    Return True if a non-deleted raw_news row already has this raw_hash.
    Used by the cleaning layer to detect cross-source title duplicates.
    """
    row = await db.fetch_one(
        "SELECT EXISTS(SELECT 1 FROM raw_news WHERE raw_hash = $1 AND is_deleted = FALSE) AS exists",
        raw_hash,
    )
    if row is None:
        return False
    return bool(row["exists"])
