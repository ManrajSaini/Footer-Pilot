import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> JSONResponse:
    try:
        await request.app.state.db.ping()
    except Exception:
        logger.exception("health check failed: database unreachable")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
