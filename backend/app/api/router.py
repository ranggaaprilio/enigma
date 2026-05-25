from fastapi import APIRouter

from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.editor import router as editor_router
from app.api.health import router as health_router
from app.api.preview import router as preview_router
from app.api.terminal import router as terminal_router
from app.api.workspaces import router as workspaces_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(health_router)
api_router.include_router(agents_router)
api_router.include_router(editor_router)
api_router.include_router(preview_router)
api_router.include_router(terminal_router)
api_router.include_router(workspaces_router)
