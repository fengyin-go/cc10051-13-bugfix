"""业务路由汇总：handoff 语义接口在前，通用资源路由兜底。"""
from __future__ import annotations

from app.routers import handoff_api, resource

ROUTERS = [handoff_api, resource]
