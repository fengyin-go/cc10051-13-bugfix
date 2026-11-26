"""记录抽取作业领域服务：管理按游标从源系统抽取主数据记录的作业调度与断点。

数据链路位置：主数据采集层，负责消费源系统登记与同步游标状态，
把抽取产物派发给标准化清洗模块。

cc10051 修复要点：
* 启动/领取前校验源系统接入状态，停用即拒绝并带回“同一份停用处置回执”；
* 已确认断点只随完整提交区间推进，失败恢复不丢位置；
* 停用期间在途作业的迟到回执进入核对台账，被终止的原提交不能确认区间；
* 恢复接入后可在原作业行从确认断点（如 800）接续，批次保留不删。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.handoff import handoff
from app.services.base import BaseModuleService
from app.store import store

START_ACTIONS = {"启动作业", "启动", "开始作业", "开始抽取", "start", "start_job", "start_with_cursor"}
CLAIM_ACTIONS = {"领取区间", "领取任务", "领取作业", "领取批次", "领取后续区间", "区间领取",
                 "领取", "claim", "claim_range", "acquire"}
RECEIPT_ACTIONS = {"完成回执", "上报回执", "提交回执", "回执上报", "上报完成回执", "接收回执",
                   "4条完成回执", "四条完成回执", "回执", "receipt", "receipts", "complete_receipt"}
SUBMIT_ACTIONS = {"提交区间", "提交抽取区间", "提交完整区间", "提交批次", "提交抽取",
                  "完整区间提交", "提交", "submit", "commit"}
COMPLETE_ACTIONS = {"完成作业", "完成抽取", "作业完成", "complete", "finish", "complete_and_advance"}
CONTINUE_ACTIONS = {"断点接续", "从断点接续", "自断点接续", "恢复作业", "继续作业", "继续抽取",
                    "接续作业", "接续", "继续", "continue", "resume"}
TERMINATE_ACTIONS = {"终止提交", "终止区间", "终止作业", "终止在途", "中止提交", "作废提交",
                     "terminate", "abort"}
ACCEPT_ACTIONS = {"接纳回执", "接纳迟到回执", "按处置接纳", "按处置继续", "确认回执完成",
                  "处置-接纳", "接纳", "确认接纳", "处置接纳", "accept"}
RECOVER_ACTIONS = {"故障恢复", "服务恢复", "失败恢复", "恢复服务", "恢复台账", "恢复状态",
                   "recover", "recovery"}
RECONCILE_ACTIONS = {"核对在途", "核对在途作业", "核对", "对账", "恢复前核对", "reconcile"}


def merge_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """把 ActionPayload 顶层额外字段、嵌套 params/data/options、remark(JSON) 合并。"""
    merged: dict[str, Any] = dict(params or {})
    for key in ("params", "data", "options", "payload"):
        nested = merged.get(key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                merged.setdefault(k, v)
    remark = merged.get("remark")
    if isinstance(remark, str):
        text = remark.strip()
        if text.startswith("{"):
            import json
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        merged.setdefault(k, v)
            except json.JSONDecodeError:
                merged.setdefault("reason", text)
        elif text:
            merged.setdefault("reason", text)
    return merged


class RecordExtractService(BaseModuleService):
    MODULE = "record_extract"
    ENTITY = "抽取作业"
    OPERATION_TERM = "抓取"
    LIST_FIELDS = ["作业名称", "源系统", "实体编码", "抽取范围", "游标位置"]
    REQUIRED_FIELDS = ["作业名称", "源系统", "实体编码"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "抽取失败"]
    ACTION_RULES = {"启动作业": "执行中", "重试抽取": "抽取失败"}
    NEGATIVE_ACTIONS = []

    # ---- 列表/详情：动态标注受理资格、确认断点、停用回执与批次状态 ----
    def _annotate(self, original: dict[str, Any]) -> dict[str, Any]:
        row = dict(original)
        code = str(row.get("源系统", ""))
        entity = str(row.get("实体编码", ""))
        row["access_open"] = handoff.is_access_open(code)
        row["受理资格"] = "可受理新作业" if row["access_open"] else "不受理（源系统已停用/接入关闭）"
        row["qualification"] = "eligible" if row["access_open"] else "closed"
        cp = handoff.checkpoint(code, entity or None)
        row["confirmed_checkpoint"] = cp
        row["确认断点"] = cp
        receipt = handoff.latest_receipt(code) if code and not handoff.is_access_open(code) else \
            handoff._stored_receipt_for(code)
        if receipt is not None:
            row["last_receipt_id"] = receipt["id"]
            row["停用回执编号"] = receipt["id"]
            row["停用处置回执"] = receipt
        batch = handoff.latest_batch_for_job(int(row.get("id", 0)))
        if batch is not None:
            row["current_batch_no"] = batch.get("batch_no")
            row["批次状态"] = batch.get("状态") or batch.get("state")
            row["batch_consumable"] = bool(batch.get("consumable"))
            row["batch_terminated"] = bool(batch.get("terminated"))
            row["记录范围"] = batch.get("记录范围")
        return row

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(**kwargs)
        return [self._annotate(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(self.MODULE, entry_id)
        return self._annotate(row) if row is not None else None

    # ---- 统一富动作入口 ----
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """兼容直接调用二元 run_action 的入口，停用校验/交接动作同样生效。"""
        special = (START_ACTIONS | CLAIM_ACTIONS | RECEIPT_ACTIONS | SUBMIT_ACTIONS
                   | COMPLETE_ACTIONS | CONTINUE_ACTIONS | TERMINATE_ACTIONS | ACCEPT_ACTIONS
                   | RECOVER_ACTIONS | RECONCILE_ACTIONS | {"重试抽取"})
        if action.strip() in special:
            outcome = self._dispatch(entry_id, action.strip(), {})
            return outcome.get("entry"), str(outcome.get("message", ""))
        return super().run_action(entry_id, action)

    def run_action_full(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._dispatch(entry_id, action.strip(), merge_params(params))

    def _dispatch(self, entry_id: int, act: str, params: dict[str, Any]) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)

        if act in RECOVER_ACTIONS:
            report = handoff.recover(params)
            report["entry"] = entry
            return report
        if act in RECONCILE_ACTIONS:
            report = handoff.recover(params)
            if entry is not None:
                batch = handoff.latest_batch_for_job(entry_id)
                report["batch"] = batch
                report["receipt"] = handoff.latest_receipt(str(entry.get("源系统", "")))
            report["entry"] = entry
            return report

        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在或已归档", "entry": None}

        if act in START_ACTIONS:
            return self._start(entry, params)
        if act in CLAIM_ACTIONS:
            result = handoff.claim_range(entry, params)
            result["entry"] = entry
            return result
        if act in RECEIPT_ACTIONS:
            result = handoff.add_receipts(entry, params)
            result["entry"] = entry
            return result
        if act in SUBMIT_ACTIONS:
            result = handoff.submit_range(entry, params)
            result["entry"] = entry
            return result
        if act in COMPLETE_ACTIONS:
            count = params.get("record_count", params.get("records", params.get("count")))
            result = self.complete_and_advance(entry_id, int(count) if count is not None else None)
            result["entry"] = entry
            return result
        if act in CONTINUE_ACTIONS:
            result = handoff.continue_job(entry, params)
            result["entry"] = entry
            return result
        if act in TERMINATE_ACTIONS:
            result = handoff.terminate_submission(
                entry, str(params.get("reason") or params.get("原因") or ""))
            result["entry"] = entry
            return result
        if act in ACCEPT_ACTIONS:
            result = handoff.accept_submission(entry, params)
            result["entry"] = entry
            return result
        if act == "重试抽取":
            return self._retry(entry, params)

        generic_entry, message = super().run_action(entry_id, act)
        return {"ok": generic_entry is not None, "message": message, "entry": generic_entry}

    def _start(self, entry: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        code = str(entry.get("源系统", "")).strip()
        denied = handoff.access_denied(code, int(entry.get("id", 0)))
        if denied is not None:
            # 关键修复：停用 CRM 后抽取作业不能启动，并返回与登记页同一份回执
            denied["entry"] = entry
            return denied
        entity = str(entry.get("实体编码", "")).strip()
        cp = handoff.checkpoint(code, entity or None)
        entry["游标位置"] = str(cp)
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["handoff_state"] = "running"
        return {"ok": True, "cursor": str(cp), "checkpoint": cp, "确认断点": cp,
                "eligible": True, "qualification": "具备领取资格",
                "受理资格": "可受理新作业", "entry": entry,
                "message": f"作业已启动，从有效确认断点 {cp} 定位；接入有效，具备区间领取资格"}

    def _retry(self, entry: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        code = str(entry.get("源系统", "")).strip()
        denied = handoff.access_denied(code, int(entry.get("id", 0)))
        if denied is not None:
            denied["entry"] = entry
            return denied
        # 被停用处置终止、保留了可接续批次的失败作业：重试=从确认断点接续
        if entry.get("handoff_state") == "terminated_resumable" or entry.get("处置结果"):
            result = handoff.continue_job(entry, params)
            result["entry"] = entry
            return result
        # 其它历史行保持基类既有动作映射（重试抽取 -> 抽取失败），不破坏通用流转
        generic_entry, message = super().run_action(int(entry["id"]), "重试抽取")
        return {"ok": generic_entry is not None, "message": message, "entry": generic_entry}

    # ---- 业务扩展：记录抽取与清洗入链 ----

    def start_with_cursor(self, entry_id: int) -> dict[str, Any]:
        """启动抽取作业，按 sync_cursor/确认断点定位起始位置；停用源系统拒绝启动。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "抽取作业不存在"}
        result = self._start(entry, {})
        if not result.get("ok"):
            return result
        return {"ok": True, "cursor": result["cursor"], "checkpoint": result["checkpoint"]}

    def complete_and_advance(self, entry_id: int, record_count: int | None = None) -> dict[str, Any]:
        """完成抽取并推进确认断点，向 std_cleanse 派发清洗任务。

        只有完整区间完成才推进；交接核心负责批次完整性与清洗资格。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "作业不存在"}
        if entry.get("status") != "执行中" and record_count is None:
            # 兼容旧契约：非执行中且未给条数时提示
            return {"ok": False, "message": "作业不在执行中状态"}
        result = handoff.complete_job(entry, record_count)
        if result.get("ok"):
            new_cursor = str(result.get("cursor", entry.get("游标位置", "0")))
            entry["游标位置"] = new_cursor
            for cur in store.rows("sync_cursor"):
                if cur.get("源系统") == entry.get("源系统"):
                    cur["游标值"] = new_cursor
                    cur["推进时间"] = datetime.now(timezone.utc).isoformat()
        return result

    def fail_with_retry(self, entry_id: int, reason: str) -> dict[str, Any]:
        """标记抽取失败并写回游标回退标记。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "抽取作业不存在"}
        entry["status"] = "抽取失败"
        entry["abnormal"] = True
        entry["handoff_state"] = "failed"
        for cur in store.rows("sync_cursor"):
            if cur.get("源系统") == entry.get("源系统"):
                cur["回退标记"] = "是"
                cur["status"] = "已回退"
        return {"ok": False, "message": f"抽取失败: {reason}", "retry_hint": "可执行重试抽取"}

    def dispatch_cleanse(self, entry_id: int) -> int:
        """把抽取结果派发给标准化清洗模块（仅服务端确认可消费的批次）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        batch = handoff.latest_batch_for_job(entry_id)
        if batch is not None and batch.get("consumable"):
            # 确认时已经派发过规则，这里只返回命中规则数
            rules = store.rows("std_cleanse")
            enabled = [r for r in rules if r.get("status") == "已启用"
                       and (not entry.get("实体编码") or r.get("实体编码") == entry.get("实体编码"))]
            return max(1, len(enabled))
        if entry.get("status") != "已完成":
            return 0
        rules = store.rows("std_cleanse")
        enabled = [r for r in rules if r.get("status") == "已启用"
                   and r.get("实体编码") == entry.get("实体编码")]
        dispatched = 0
        for rule in enabled:
            rule.setdefault("待清洗批次", []).append(entry.get("作业名称", ""))
            dispatched += 1
        return dispatched

    def consume_source_config(self, entry_id: int) -> dict[str, Any] | None:
        """读取源系统登记信息，确认抽取目标可用（必须未停用）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        sys_code = entry.get("源系统", "")
        if not handoff.is_access_open(sys_code):
            return None
        return next((s for s in store.rows("source_system")
                     if s.get("系统标识") == sys_code and s.get("status") == "已接入"), None)

    def batch_extract(self, sys_code: str, entity_code: str, ranges: list[str]) -> list[int]:
        """批量创建抽取作业，一次提交整组作业。接入关闭时不创建可领取作业。"""
        if not handoff.is_access_open(sys_code):
            return []
        jobs = store.rows(self.MODULE)
        created: list[int] = []
        for rng in ranges:
            job_id = max((int(r.get("id", 0)) for r in jobs), default=0) + 1
            jobs.append({
                "id": job_id,
                "作业名称": f"batch-{entity_code}-{job_id}",
                "源系统": sys_code,
                "实体编码": entity_code,
                "抽取范围": rng,
                "游标位置": str(handoff.checkpoint(sys_code, entity_code)),
                "status": "待执行",
                "pending": True,
                "abnormal": False,
            })
            created.append(job_id)
        return created
