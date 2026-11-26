"""源系统登记领域服务：管理主数据来源系统的登记、接入激活、停用与责任边界维护。

数据链路位置：主数据治理链路源头，负责登记源系统并把接入信号传递给
抽取作业与同步游标，是抽取链路的第一个上游。

cc10051 修复：停用动作开具“停用处置回执”，接入/停用结论统一交给
app.handoff 交接核心，记录抽取页拿到的是同一份回执。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.handoff import handoff
from app.services.base import BaseModuleService
from app.store import store

STOP_ACTIONS = {"停用系统", "确认停用", "提交停用", "停用确认", "stop", "deactivate", "disable"}
START_ACCESS_ACTIONS = {"接入系统", "恢复接入", "重新接入", "确认接入", "reactivate", "activate", "enable"}
DISPOSE_ACTIONS = {"处置停用单", "确认处置", "停用处置", "出具处置结论", "resolve_disposition", "dispose"}


class SourceSystemService(BaseModuleService):
    MODULE = "source_system"
    ENTITY = "源系统"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["系统标识", "系统名称", "数据域", "同步频率", "责任人"]
    REQUIRED_FIELDS = ["系统标识", "系统名称", "数据域"]
    STATUS_ORDER = ["待接入", "已接入", "已停用"]
    ACTION_RULES = {"接入系统": "已接入", "停用系统": "已停用"}
    NEGATIVE_ACTIONS = ["停用系统"]

    # ---- 列表/详情：附带接入/停用回执信息，方便登记页直接辨认 ----
    def _annotate(self, original: dict[str, Any]) -> dict[str, Any]:
        row = dict(original)
        code = str(row.get("系统标识", ""))
        row["access_open"] = handoff.is_access_open(code)
        row["接入资格"] = "可接入/可受理" if row["access_open"] else "已关闭（新作业不受理）"
        receipt = handoff.latest_receipt(code) if code else None
        if receipt is not None:
            row["last_receipt_id"] = receipt["id"]
            row["停用回执编号"] = receipt["id"]
            row["停用处置回执"] = receipt
        return row

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(**kwargs)
        return [self._annotate(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(self.MODULE, entry_id)
        return self._annotate(row) if row is not None else None

    # ---- 统一动作入口 ----
    def create_entry(self, values: dict[str, Any]):
        entry, missing = super().create_entry(values)
        # 允许调用方在登记时显式指定合法状态（如直接“已接入”），缺省仍是“待接入”
        if entry is not None:
            explicit = values.get("status") or values.get("状态")
            if explicit in self.STATUS_ORDER:
                entry["status"] = explicit
            if entry.get("status") == "已接入":
                handoff.system_access[str(entry.get("系统标识", ""))] = True
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """兼容直接调用二元 run_action 的入口，同样开具/返回停用回执。"""
        if action in STOP_ACTIONS or action in START_ACCESS_ACTIONS or action in DISPOSE_ACTIONS:
            outcome = self.run_action_full(entry_id, action, {})
            return outcome.get("entry"), str(outcome.get("message", ""))
        return super().run_action(entry_id, action)

    def run_action_full(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = params or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在或已归档", "entry": None}

        if action in DISPOSE_ACTIONS:
            code = str(entry.get("系统标识", ""))
            decision = str(params.get("decision") or params.get("disposition")
                           or params.get("处置结论") or params.get("在途处置") or "")
            target = params.get("receipt_id") or params.get("receipt_no") or code
            result = handoff.resolve_receipt(str(target), decision, params)
            result.setdefault("entry", entry)
            return result

        if action in STOP_ACTIONS:
            reason = str(params.get("reason") or params.get("原因")
                         or params.get("remark") or params.get("停用原因") or "")
            result = handoff.deactivate(entry, reason=reason, params=params)
            result.setdefault("entry", entry)
            return result

        if action in START_ACCESS_ACTIONS:
            result = handoff.reactivate(entry, reason=str(params.get("reason") or ""))
            return result

        return super().run_action_full(entry_id, action, params)

    # ---- 业务扩展：源系统接入与抽取联动 ----

    def register_and_activate(self, system_id: str, name: str, domain: str, owner: str) -> dict[str, Any]:
        """登记并激活源系统，写入源系统数据集。"""
        rows = store.rows(self.MODULE)
        if any(r.get("系统标识") == system_id for r in rows):
            return {"ok": False, "message": f"源系统 {system_id} 已登记"}
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({"系统标识": system_id, "系统名称": name, "数据域": domain,
                      "同步频率": "每日", "责任人": owner, "status": "已接入",
                      "pending": False, "abnormal": False})
        rows.append(entry)
        handoff.system_access[system_id] = True
        return {"ok": True, "entry_id": entry["id"], "entry": entry,
                "message": f"源系统 {system_id} 已接入"}

    def push_extract_job(self, entry_id: int) -> dict[str, Any]:
        """为已接入的源系统创建抽取作业，形成接入→抽取链路。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已接入":
            return {"ok": False, "message": "源系统不存在或未接入"}
        code = str(entry.get("系统标识", ""))
        if not handoff.is_access_open(code):
            denied = handoff.access_denied(code)
            return denied or {"ok": False, "message": "源系统接入已关闭，不再发放领取资格"}
        jobs = store.rows("record_extract")
        job_id = max((int(r.get("id", 0)) for r in jobs), default=0) + 1
        jobs.append({
            "id": job_id,
            "作业名称": f"抽取-{entry.get('系统标识')}-{job_id}",
            "源系统": entry.get("系统标识", ""),
            "实体编码": "",
            "抽取范围": "全量",
            "游标位置": str(handoff.checkpoint(code)),
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "job_id": job_id,
                "cursor": handoff.checkpoint(code),
                "message": f"已创建抽取作业 {job_id}，从确认断点 {handoff.checkpoint(code)} 开始"}

    def ensure_cursor(self, entry_id: int) -> dict[str, Any]:
        """为源系统初始化同步游标，写入 sync_cursor 模块。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "源系统不存在"}
        code = str(entry.get("系统标识", ""))
        existing = next((c for c in store.rows("sync_cursor") if c.get("源系统") == code), None)
        if existing is not None:
            return {"ok": True, "cursor_id": existing.get("id"), "cursor": existing}
        cursors = store.rows("sync_cursor")
        cursor_id = max((int(r.get("id", 0)) for r in cursors), default=0) + 1
        cursors.append({
            "id": cursor_id,
            "源系统": code,
            "实体编码": "",
            "游标值": "0",
            "推进时间": datetime.now(timezone.utc).isoformat(),
            "回退标记": "否",
            "status": "正常",
            "pending": False,
            "abnormal": False,
        })
        return {"ok": True, "cursor_id": cursor_id}

    def stop_and_disconnect(self, entry_id: int, reason: str) -> str:
        """停用源系统并把关联游标记为停滞；开具停用处置回执（保留旧签名返回字符串）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "源系统不存在"
        result = handoff.deactivate(entry, reason=reason)
        receipt = result.get("receipt") or {}
        return f"源系统已停用，原因: {reason}；{receipt.get('message', '')}"

    def stop_with_receipt(self, entry_id: int, reason: str = "",
                          params: dict[str, Any] | None = None) -> dict[str, Any]:
        """富返回版本：直接给出同一份停用处置回执。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "源系统不存在"}
        return handoff.deactivate(entry, reason=reason, params=params)

    def list_by_domain(self, domain: str) -> list[dict[str, Any]]:
        """按数据域筛选源系统，供血缘登记使用。"""
        return [r for r in store.rows(self.MODULE) if domain in str(r.get("数据域", ""))]

    def register_lineage_edge(self, entry_id: int, downstream: str) -> str | None:
        """登记源系统到下游节点的血缘边。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        edges = store.rows("lineage_graph")
        edge_id = max((int(r.get("id", 0)) for r in edges), default=0) + 1
        edges.append({
            "id": edge_id,
            "血缘边标识": f"edge-{entry_id}-{edge_id}",
            "上游节点": entry.get("系统标识", ""),
            "下游节点": downstream,
            "变换规则": "直接同步",
            "登记时间": datetime.now(timezone.utc).isoformat(),
            "status": "已登记",
            "pending": False,
            "abnormal": False,
        })
        return f"edge-{entry_id}-{edge_id}"
