"""Aggregates all API routers under /api/v1."""

from fastapi import APIRouter

from app.api.routes import auth, github, health, projects, repositories, review, system, tasks

api_router = APIRouter()
for module in (health, auth, projects, repositories, tasks, review, github, system):
    api_router.include_router(module.router)
