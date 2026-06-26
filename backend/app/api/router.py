from fastapi import APIRouter

from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.commands import router as commands_router
from app.api.dashboard import router as dashboard_router
from app.api.dev_server import router as dev_server_router
from app.api.editor import router as editor_router
from app.api.files import router as files_router
from app.api.health import router as health_router
from app.api.notifications import router as notifications_router
from app.api.preview import router as preview_router
from app.api.projects import router as projects_router
from app.api.teams import router as teams_router
from app.api.terminal import router as terminal_router
from app.api.workspaces import router as workspaces_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(health_router)
api_router.include_router(dashboard_router)
api_router.include_router(notifications_router)
api_router.include_router(agents_router)
api_router.include_router(commands_router)
api_router.include_router(dev_server_router)
api_router.include_router(editor_router)
api_router.include_router(files_router)
api_router.include_router(preview_router)
api_router.include_router(projects_router)
api_router.include_router(teams_router)
api_router.include_router(terminal_router)
api_router.include_router(workspaces_router)
