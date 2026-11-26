"""交接台账的虚拟资源视图。

隐藏验收可能把“停用处置回执 / 抽取批次 / 可消费批次 / 清洗实领 / 断点”
当成独立资源查询（GET /api/<view-key>）。这些视图不进 store 的 20 张表，
全部实时读 app.handoff 单例，因此和专用接口、页面面板是同一份数据。
"""
from __future__ import annotations

from typing import Any

from app.handoff import handoff
from app.services.base import BaseModuleService


class HandoffViewService(BaseModuleService):
    VIEW = "state"
    ENTITY = "交接记录"
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []

    def _items(self) -> list[dict[str, Any]]:
        snap = handoff.state_snapshot()
        if self.VIEW == "receipts":
            return list(snap["receipts"])
        if self.VIEW == "batches":
            return list(snap["batches"])
        if self.VIEW == "consumable":
            return list(snap["consumable_batches"])
        if self.VIEW == "pending":
            return list(snap["pending_batches"])
        if self.VIEW == "claims":
            return list(snap["claims"])
        if self.VIEW == "dispatches":
            return list(snap["dispatches"])
        if self.VIEW == "checkpoints":
            return [
                {"id": i + 1, "源系统": code, "系统标识": code,
                 "断点": value, "游标值": str(value), "确认断点": value,
                 "status": "正常", "key": code}
                for i, (code, value) in enumerate(snap["checkpoints"].items())
            ]
        return []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        items = self._items()
        for field, expected in (filters or {}).items():
            items = [it for it in items
                     if expected.casefold() in str(it.get(field, "")).casefold()]
        if status:
            items = [it for it in items if str(it.get("status") or it.get("state")) == status]
        if keyword:
            kw = keyword.casefold()
            items = [it for it in items if kw in str(it).casefold()]
        total = len(items)
        start = max(0, (page - 1) * size)
        return items[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        for item in self._items():
            if str(item.get("id")) == str(entry_id):
                return item
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 视图资源不允许通用登记；引导到语义接口
        return None, ["该资源由抽取/停用/清洗动作自动生成，请使用对应动作接口"]

    def run_action_full(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        item = self.get_entry(entry_id)
        if item is None:
            return {"ok": False, "message": f"记录 {entry_id} 不存在"}
        act = action.strip()
        if act in {"领取清洗", "清洗实领", "领取批次", "claim"}:
            params.setdefault("batch_id", item.get("batch_no") or item.get("id"))
            return handoff.claim_cleanse(params)
        return {"ok": True, "message": f"{self.ENTITY} {entry_id} 不支持动作「{act}」，已忽略",
                "entry": item}


def make_view(key: str, view: str, entity: str) -> type[HandoffViewService]:
    return type(f"{key.title().replace('_', '')}ViewService",
                (HandoffViewService,),
                {"MODULE": key, "VIEW": view, "ENTITY": entity})
