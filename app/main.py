from fastapi import FastAPI
from app.core.config import settings
from app.api.routes import calls, health

app = FastAPI(title=settings.app_name + " v2", debug=settings.debug)

app.include_router(health.router, prefix="/health", tags=["health"])
app.include_router(calls.router, prefix="/calls", tags=["calls"])
