from fastapi import APIRouter, status

from app.api.schemas import ErrorResponse, HKStockListSyncResponse
from app.common.hk_stock_list import refresh_hk_stock_list

router = APIRouter()


@router.post(
    "/admin/hk-stock-list-sync",
    response_model=HKStockListSyncResponse,
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def post_hk_stock_list_sync() -> HKStockListSyncResponse:
    """Reload the HK Stock List and return how many entries were loaded."""
    entries_loaded = await refresh_hk_stock_list()
    return HKStockListSyncResponse(entries_loaded=entries_loaded)
