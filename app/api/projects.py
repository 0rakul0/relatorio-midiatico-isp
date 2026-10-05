from fastapi import APIRouter

from app.api.project_create import router as create_router
from app.api.project_preview import router as preview_router


router = APIRouter()
router.include_router(create_router)
router.include_router(preview_router)
