"""源系统交接 → 记录抽取 → 标准化清洗 的共享领域核心。

题位 cc10051 的修复都收敛在这里，保证：

* 源系统登记页与记录抽取页看到的是**同一份停用处置回执**（按源系统标识取最新单）；
* 接入关闭后不再给新作业/新区间发领取资格，在途作业的迟到回执仍会被核对留痕；
* 已确认断点只来自“已经提交且完整的抽取区间”，半段读入/被终止的候补区间不计入；
* 一张停用单只收尾一个源系统标识，CRM 与 ERP 的断点、批次互不串扰；
* 清洗只能领取服务端确认可消费的完整批次，并保留清洗实领记录；
* 失败恢复（recover）按持久化的确认批次/游标重算断点，不丢已确认位置。

本模块不依赖 FastAPI，路由层与各模块服务都调用同一份单例，避免两个入口各说各话。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.store import store

STATE_FILE = Path(__file__).resolve().parents[1] / ".handoff-state.json"

# 处置结论取值（同时兼容中文写法）
DECISION_TERMINATE = "terminate"
DECISION_ACCEPT = "accept"

_TERMINATE_WORDS = ("终止", "终结", "驳回", "拒绝", "作废", "terminate", "abort", "reject", "cancel")
_ACCEPT_WORDS = ("接纳", "接受", "继续", "确认完成", "确认", "放行", "accept", "continue", "commit")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_decision(value: Any, default: str = DECISION_TERMINATE) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if any(word.lower() in text for word in _TERMINATE_WORDS):
        return DECISION_TERMINATE
    if any(word.lower() in text for word in _ACCEPT_WORDS):
        return DECISION_ACCEPT
    return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        text = str(value).strip()
        # 允许 "800"、"800.0" 这类写法
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _system_code(entry_or_code: Any) -> str:
    if isinstance(entry_or_code, dict):
        return str(entry_or_code.get("系统标识") or entry_or_code.get("源系统") or "").strip()
    return str(entry_or_code or "").strip()


class HandoffCore:
    """内存态交接台账（与 store 同样的仓库生命周期），可选 write-through 快照。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.receipts: dict[str, dict[str, Any]] = {}
        self.receipt_order: list[str] = []
        self.batches: dict[str, dict[str, Any]] = {}
        self.batch_order: list[str] = []
        self.dispatches: list[dict[str, Any]] = []
        self.claims: dict[str, dict[str, Any]] = {}
        self.claim_order: list[str] = []
        # 显式确认过的断点：{ "CRM": 800 }，恢复时与确认批次/游标一起重算
        self.confirmed_positions: dict[str, int] = {}
        self.system_access: dict[str, bool] = {}
        self._seq = {"receipt": 0, "batch": 0, "claim": 0, "dispatch": 0}
        self._load()

    # ------------------------------------------------------------------
    # 持久化：真实进程失败/重启后仍能拿回已确认位置（快照可删除以恢复出厂）
    # ------------------------------------------------------------------
    def _load(self) -> None:
        data: Any = None
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema") != 1:
            return
        # 真实进程重启时连同内存业务表一起还原，保证 CRM 登记行、抽取作业行仍在，
        # “服务端失败恢复”后可以从原作业行/原断点继续。
        tables = data.get("store_tables")
        if isinstance(tables, dict):
            store._tables.clear()
            for name, rows in tables.items():
                if isinstance(rows, list):
                    store._tables[str(name)] = rows
        self.receipts = data.get("receipts", {})
        self.receipt_order = data.get("receipt_order", [])
        self.batches = data.get("batches", {})
        self.batch_order = data.get("batch_order", [])
        self.dispatches = data.get("dispatches", [])
        self.claims = data.get("claims", {})
        self.claim_order = data.get("claim_order", [])
        self.confirmed_positions = {str(k): int(v) for k, v in data.get("confirmed_positions", {}).items()}
        self.system_access = {str(k): bool(v) for k, v in data.get("system_access", {}).items()}
        self._seq = data.get("seq", {"receipt": 0, "batch": 0, "claim": 0, "dispatch": 0})

    def _persist(self) -> None:
        try:
            payload = {
                "schema": 1,
                "saved_at": now_iso(),
                "store_tables": store._tables,
                "receipts": self.receipts,
                "receipt_order": self.receipt_order,
                "batches": self.batches,
                "batch_order": self.batch_order,
                "dispatches": self.dispatches,
                "claims": self.claims,
                "claim_order": self.claim_order,
                "confirmed_positions": self.confirmed_positions,
                "system_access": self.system_access,
                "seq": self._seq,
            }
            tmp = STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except (OSError, TypeError, ValueError):
            # 快照只是加固，不能因为只读文件系统/序列化问题影响主流程
            pass

    def reset(self) -> dict[str, Any]:
        """清空交接台账（测试/演练复位用），不动 20 个业务模块的 seed 数据。"""
        with self.lock:
            self.receipts.clear()
            self.receipt_order.clear()
            self.batches.clear()
            self.batch_order.clear()
            self.dispatches.clear()
            self.claims.clear()
            self.claim_order.clear()
            self.confirmed_positions.clear()
            self.system_access.clear()
            self._seq = {"receipt": 0, "batch": 0, "claim": 0, "dispatch": 0}
            try:
                STATE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": True, "message": "交接台账已复位"}

    # ------------------------------------------------------------------
    # 源系统查询
    # ------------------------------------------------------------------
    def source_row(self, code: str) -> dict[str, Any] | None:
        code = _system_code(code)
        if not code:
            return None
        rows = [s for s in store.rows("source_system") if str(s.get("系统标识", "")) == code]
        if not rows:
            return None
        # 同标识重复登记时以最新一行的状态为准
        return max(rows, key=lambda r: int(r.get("id", 0)))

    def is_access_open(self, code: str) -> bool:
        """接入资格：只有显式停用/关闭才拒绝；查不到登记的历史样例保持兼容放行。"""
        code = _system_code(code)
        if not code:
            return True
        if code in self.system_access:
            return self.system_access[code]
        row = self.source_row(code)
        if row is not None and str(row.get("status", "")) == "已停用":
            return False
        return True

    def _stored_receipt_for(self, code: str) -> dict[str, Any] | None:
        """只查台账里已经开具的停用单，不做补开（避免与 deactivate 递归）。"""
        code = _system_code(code)
        for rid in reversed(self.receipt_order):
            rc = self.receipts[rid]
            if rc.get("system") == code or code in (rc.get("scope_systems") or []):
                return rc
        return None

    def latest_receipt(self, code: str) -> dict[str, Any] | None:
        """返回停用单：优先台账里正式开具的同一份回执。

        对“库里已是已停用、但台账无单”的历史/样例行，给一份**派生的、
        不落库不占号**的回执，保证两个入口仍能展示同一编号；正式停用动作
        开具的回执始终是可核对的持久化对象。
        """
        code = _system_code(code)
        stored = self._stored_receipt_for(code)
        if stored is not None:
            return stored
        row = self.source_row(code)
        if row is not None and str(row.get("status")) == "已停用":
            cp = self.checkpoint(code)
            rid = f"STOP-{code}-DERIVED"
            return {
                "id": rid,
                "receipt_id": rid,
                "回执编号": rid,
                "停用单号": rid,
                "kind": "derived_disposal_receipt",
                "system": code,
                "系统标识": code,
                "source_entry_id": row.get("id"),
                "status": "已停用",
                "access_open": False,
                "new_job_accepted": False,
                "新作业受理": "关闭（不予受理新作业/新区间领取）",
                "scope_systems": [code],
                "收尾范围": f"仅源系统 {code}",
                "checkpoint_at_stop": cp,
                "confirmed_checkpoint": cp,
                "确认断点": cp,
                "jobs": [],
                "在途作业处置": [],
                "迟到回执核对": {"total": 0, "accepted": 0, "rejected": 0},
                "message": f"源系统「{code}」处于已停用状态（历史登记），新作业不予受理；"
                           f"当前确认断点 {cp}。该回执为按登记状态派生。",
            }
        return None

    # ------------------------------------------------------------------
    # 断点
    # ------------------------------------------------------------------
    def cursor_value(self, code: str, entity: str | None = None) -> int:
        """从 sync_cursor 行读取数值断点；停用导致的“游标停滞”不影响读值。"""
        best = 0
        for cur in store.rows("sync_cursor"):
            if str(cur.get("源系统", "")) != code:
                continue
            if entity and cur.get("实体编码") and str(cur.get("实体编码")) != entity:
                continue
            best = max(best, to_int(cur.get("游标值"), 0))
        return best

    def checkpoint(self, code: str, entity: str | None = None) -> int:
        """当前有效确认断点 = max(显式确认位, 游标值, 已确认完整批次的右端)。"""
        with self.lock:
            value = int(self.confirmed_positions.get(code, 0))
        value = max(value, self.cursor_value(code, entity))
        for batch in self.batches.values():
            if batch.get("system") != code:
                continue
            if entity and batch.get("entity") and str(batch.get("entity")) != entity:
                continue
            if batch.get("confirmed") and not batch.get("terminated"):
                value = max(value, to_int(batch.get("range_end"), 0))
        return value

    def save_checkpoint(self, code: str, value: int, entity: str | None = None) -> int:
        value = max(0, int(value))
        with self.lock:
            self.confirmed_positions[code] = max(int(self.confirmed_positions.get(code, 0)), value)
        # 同步到 sync_cursor（保留既有行，停用状态下也只改值与恢复标记）
        rows = [c for c in store.rows("sync_cursor") if str(c.get("源系统", "")) == code]
        if entity:
            scoped = [c for c in rows if not c.get("实体编码") or str(c.get("实体编码")) == entity]
            rows = scoped or rows
        now = now_iso()
        if rows:
            for cur in rows:
                cur["游标值"] = str(value)
                cur["推进时间"] = now
                cur["回退标记"] = "否"
                if str(cur.get("status")) != "游标停滞":
                    cur["status"] = "正常"
        else:
            cursors = store.rows("sync_cursor")
            cid = max((to_int(r.get("id")) for r in cursors), default=0) + 1
            cursors.append({
                "id": cid,
                "源系统": code,
                "实体编码": entity or "",
                "游标值": str(value),
                "推进时间": now,
                "回退标记": "否",
                "status": "正常",
                "pending": False,
                "abnormal": False,
            })
        self._persist()
        return value

    # ------------------------------------------------------------------
    # 停用 / 接入交接，开具同一份停用处置回执
    # ------------------------------------------------------------------
    def deactivate(
        self,
        source: dict[str, Any] | str,
        *,
        reason: str = "",
        decision: str | None = None,
        params: dict[str, Any] | None = None,
        _row_already_stopped: bool = False,
    ) -> dict[str, Any]:
        params = params or {}
        if isinstance(source, dict):
            row = source
            code = str(row.get("系统标识", "")).strip()
        else:
            code = str(source).strip()
            row = self.source_row(code)
            if row is None:
                return {"ok": False, "message": f"源系统 {code} 不存在，无法停用"}

        with self.lock:
            # 幂等：同系统台账里已有停用单就返回同一份回执
            existed = self._stored_receipt_for(code)
            if existed is not None and str(row.get("status")) == "已停用" and not params.get("force_new"):
                return {"ok": True, "receipt": existed, "receipt_id": existed["id"],
                        "message": f"源系统 {code} 已停用，沿用停用处置回执 {existed['id']}"}

            chosen = parse_decision(
                decision if decision is not None
                else params.get("decision") or params.get("disposition")
                or params.get("处置结论") or params.get("在途处置")
                or params.get("处置方式") or params.get("remark"),
                DECISION_TERMINATE,
            )

            row["status"] = "已停用"
            row["abnormal"] = True
            row["pending"] = False
            if reason:
                row["停用原因"] = reason
            stop_at = self.checkpoint(code)

            # 游标停滞但保留断点值
            for cur in store.rows("sync_cursor"):
                if str(cur.get("源系统", "")) == code:
                    cur["status"] = "游标停滞"
                    cur["pending"] = True

            self.system_access[code] = False

            # 收尾本系统在途作业/批次（绝不碰其它源系统，例如 ERP）
            job_dispositions: list[dict[str, Any]] = []
            for job in store.rows("record_extract"):
                if str(job.get("源系统", "")) != code:
                    continue
                job_id = int(job.get("id", 0))
                active_batch = self._active_batch_for_job(job_id)
                report_batch = active_batch or self.latest_batch_for_job(job_id)
                item: dict[str, Any] = {
                    "job_id": job.get("id"),
                    "job_name": job.get("作业名称", ""),
                    "batch_id": report_batch["id"] if report_batch else None,
                    "batch_no": report_batch.get("batch_no") if report_batch else None,
                }
                if active_batch is None:
                    if report_batch is not None and report_batch.get("confirmed"):
                        item.update({"state": "confirmed",
                                     "result": "完整区间已确认，批次已派发/可领取清洗，停用不影响该批次"})
                        job_dispositions.append(item)
                    elif str(job.get("status")) == "执行中":
                        # 没有结构化批次的老作业：按停用收尾，断点不前进
                        job["handoff_state"] = "stopped_unconfirmed"
                        item.update({"state": "stopped_unconfirmed",
                                     "result": "执行中作业被停用交接冻结，可自确认断点接续"})
                        job_dispositions.append(item)
                    continue

                if active_batch.get("confirmed"):
                    item.update({"state": "confirmed",
                                 "result": "完整区间已确认，批次可派发清洗"})
                elif chosen == DECISION_ACCEPT and self._batch_complete(active_batch):
                    self._confirm_batch(active_batch, by="disposition-accept")
                    item.update({"state": "accepted",
                                 "result": "按处置结论接纳迟到回执，完整区间已确认"})
                else:
                    # 默认：终止原提交，保留批次与确认断点，等待重新接入后续作。
                    # 接续点只认真实已确认水位，不能取某个候补区间的起点，
                    # 否则 804-808 的半段区间会把断点错误抬到 804。
                    resume_at = self.checkpoint(code)
                    active_batch["terminated"] = True
                    active_batch["state"] = "terminated_resumable"
                    active_batch["状态"] = "停用处置：原提交已终止·批次保留可接续"
                    active_batch["terminate_reason"] = reason or "源系统停用，原提交被终止"
                    active_batch["resume_from"] = resume_at
                    job["status"] = "抽取失败"
                    job["abnormal"] = True
                    job["pending"] = True
                    job["handoff_state"] = "terminated_resumable"
                    job["处置结果"] = f"原提交已终止，保留可接续批次，自断点 {resume_at} 接续"
                    item.update({"state": "terminated_resumable",
                                 "result": job["处置结果"],
                                 "resume_from": resume_at})
                job_dispositions.append(item)

            self._seq["receipt"] += 1
            rid = str(params.get("receipt_id") or params.get("receipt_no") or "").strip() \
                or f"TZ-{code or 'SYS'}-{self._seq['receipt']:03d}"
            receipt = {
                "id": rid,
                "receipt_id": rid,
                "receipt_no": rid,
                "回执编号": rid,
                "停用单号": rid,
                "kind": "source_disposal_receipt",
                "system": code,
                "系统标识": code,
                "source_entry_id": row.get("id"),
                "action": "停用系统",
                "status": "已停用",
                "created_at": now_iso(),
                "reason": reason or str(params.get("remark") or ""),
                "decision": chosen,
                "处置结论": "终止原提交并保留可接续批次" if chosen == DECISION_TERMINATE else "接纳在途回执并核对确认",
                "access_open": False,
                "new_job_accepted": False,
                "新作业受理": "关闭（不予受理新作业/新区间领取）",
                "scope_systems": [code],
                "收尾范围": f"仅源系统 {code} 的抽取作业与批次",
                "checkpoint_at_stop": stop_at,
                "confirmed_checkpoint": stop_at,
                "确认断点": stop_at,
                "jobs": job_dispositions,
                "在途作业处置": job_dispositions,
                "late_receipts": [],
                "迟到回执核对": {"total": 0, "accepted": 0, "rejected": 0},
                "message": "",
            }
            receipt["message"] = (
                f"源系统「{code}」停用处置回执 {rid}：接入已关闭，新作业/新区间不予领取；"
                f"确认断点 {stop_at}；本单仅收尾 {code}，在途作业 {len(job_dispositions)} 个"
                + ("，未确认提交已终止，批次保留可接续" if chosen == DECISION_TERMINATE else "，按接纳迟到回执核对")
            )
            self.receipts[rid] = receipt
            self.receipt_order.append(rid)
            row["last_receipt_id"] = rid
            row["停用回执编号"] = rid
            row["停用处置回执"] = receipt
            self._persist()
            return {"ok": True, "receipt": receipt, "receipt_id": rid,
                    "disposal_receipt": receipt, "disposal_receipt_id": rid,
                    "停用处置回执": receipt, "停用回执编号": rid,
                    "entry": row, "message": receipt["message"]}

    def reactivate(self, source: dict[str, Any] | str, *, reason: str = "") -> dict[str, Any]:
        if isinstance(source, dict):
            row = source
            code = str(row.get("系统标识", "")).strip()
        else:
            code = str(source).strip()
            row = self.source_row(code)
            if row is None:
                return {"ok": False, "message": f"源系统 {code} 不存在，无法接入"}
        with self.lock:
            row["status"] = "已接入"
            row["abnormal"] = False
            row["pending"] = False
            self.system_access[code] = True
            for cur in store.rows("sync_cursor"):
                if str(cur.get("源系统", "")) == code and str(cur.get("status")) == "游标停滞":
                    cur["status"] = "正常"
                    cur["pending"] = False
            cp = self.checkpoint(code)
            resumable = [
                {"job_id": j.get("id"), "job_name": j.get("作业名称", ""),
                 "resume_from": b.get("resume_from", cp), "batch_id": b.get("id")}
                for j in store.rows("record_extract") if str(j.get("源系统", "")) == code
                for b in [self._active_batch_for_job(int(j.get("id", 0)))]
                if b is not None and b.get("terminated")
            ]
            msg = f"源系统「{code}」已恢复接入，新作业可受理；使用上次完成的有效断点 {cp} 继续采集"
            if resumable:
                msg += f"；{len(resumable)} 个原作业可从原行自断点接续"
            self._persist()
            return {"ok": True, "entry": row, "checkpoint": cp, "确认断点": cp,
                    "resumable_jobs": resumable, "可接续作业": resumable, "message": msg}

    def access_denied(self, code: str, entry_id: int | None = None) -> dict[str, Any] | None:
        """若接入关闭，返回标准化的拒绝结果（携带同一份停用处置回执）；否则 None。"""
        code = _system_code(code)
        if self.is_access_open(code):
            return None
        receipt = self.latest_receipt(code)
        rid = receipt["id"] if receipt else ""
        cp = self.checkpoint(code)
        message = (
            f"源系统「{code}」已停用/关闭接入，抽取作业启动与区间领取不予受理。"
            f"停用处置回执 {rid}（同一份回执，源系统登记页与记录抽取页均可核对）："
            f"确认断点 {cp}，关闭期间不发领取资格；恢复接入后可从确认断点 {cp} 接续。"
        )
        return {
            "ok": False,
            "code": "SOURCE_ACCESS_CLOSED",
            "eligible": False,
            "qualification": "不具备领取资格（接入已关闭）",
            "领取资格": "不予发放",
            "message": message,
            "receipt": receipt,
            "receipt_id": rid,
            "停用处置回执": receipt,
            "checkpoint": cp,
            "确认断点": cp,
            "entry": store.find("record_extract", entry_id) if entry_id else None,
        }

    # ------------------------------------------------------------------
    # 抽取批次 / 区间 / 回执
    # ------------------------------------------------------------------
    def _new_batch_no(self) -> str:
        self._seq["batch"] += 1
        return f"EXB-{self._seq['batch']:04d}"

    def _active_batch_for_job(self, job_id: int) -> dict[str, Any] | None:
        """返回该作业当前可继续回执/提交的批次：优先最新的未确认且未终止批次。"""
        fallback: dict[str, Any] | None = None
        for bid in reversed(self.batch_order):
            b = self.batches[bid]
            if to_int(b.get("job_id"), -1) != job_id or b.get("confirmed"):
                continue
            if not b.get("terminated"):
                return b
            if fallback is None:
                fallback = b
        return fallback

    def latest_batch_for_job(self, job_id: int) -> dict[str, Any] | None:
        for bid in reversed(self.batch_order):
            b = self.batches[bid]
            if to_int(b.get("job_id"), -1) == job_id:
                return b
        return None

    def claim_range(self, job: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        code = str(job.get("源系统", "")).strip()
        denied = self.access_denied(code, to_int(job.get("id")))
        if denied is not None:
            return denied

        entity = str(job.get("实体编码", "")).strip()
        cp = self.checkpoint(code, entity or None)
        start = params.get("start", params.get("start_offset", params.get("from", params.get("起点"))))
        end = params.get("end", params.get("to", params.get("终点")))
        width = params.get("limit", params.get("count", params.get("record_count",
                          params.get("records", params.get("条数", params.get("宽度"))))))
        start_v = cp if start is None else to_int(start, cp)
        if end is not None:
            end_v = to_int(end, start_v)
        elif width is not None:
            end_v = start_v + max(0, to_int(width, 0))
        else:
            # 页面快捷操作默认领取断点后的 4 条区间，呼应“4 条完成回执”演练
            end_v = start_v + 4

        with self.lock:
            batch_id = max((to_int(b.get("id")) for b in self.batches.values()), default=0) + 1
            batch_no = self._new_batch_no()
            batch = {
                "id": batch_id,
                "batch_id": batch_id,
                "batch_no": batch_no,
                "批次号": batch_no,
                "job_id": job.get("id"),
                "作业ID": job.get("id"),
                "job_name": job.get("作业名称", ""),
                "system": code,
                "源系统": code,
                "entity": entity,
                "实体编码": entity,
                "range_start": start_v,
                "range_end": end_v,
                "记录范围": f"[{start_v}, {end_v})",
                "抽取范围": f"{start_v}-{end_v}",
                "expected_count": max(0, end_v - start_v),
                "receipt_count": 0,
                "record_count": 0,
                "receipts": [],
                "claimed": True,
                "submitted": False,
                "complete": False,
                "confirmed": False,
                "consumable": False,
                "terminated": False,
                "state": "claimed",
                "状态": "已领取区间/半段读入",
                "claimed_at": now_iso(),
            }
            self.batches[batch_no] = batch
            self.batch_order.append(batch_no)
            job["status"] = "执行中"
            job["pending"] = True
            job["abnormal"] = False
            job["游标位置"] = str(start_v)
            job["抽取范围"] = batch["抽取范围"]
            job["claimed_range"] = batch["记录范围"]
            job["current_batch_no"] = batch_no
            job["handoff_state"] = "claimed"
            self._persist()
            return {"ok": True, "batch": batch, "batch_id": batch_id, "batch_no": batch_no,
                    "checkpoint": cp, "确认断点": cp, "eligible": True,
                    "qualification": "具备领取资格",
                    "message": f"作业已从确认断点 {cp} 领取区间 {batch['记录范围']}（{batch_no}），提交完整前不算完成"}

    def _resolve_batch(self, job: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any] | None:
        ref = params.get("batch_id") or params.get("batch_no") or params.get("批次") or params.get("批次号")
        if ref is not None and str(ref).strip():
            key = str(ref).strip()
            if key in self.batches:
                return self.batches[key]
            for b in self.batches.values():
                if str(b.get("id")) == key or str(b.get("batch_id")) == key:
                    return b
        if job is not None:
            active = self._active_batch_for_job(to_int(job.get("id")))
            if active is not None:
                return active
            return self.latest_batch_for_job(to_int(job.get("id")))
        return None

    def _batch_complete(self, batch: dict[str, Any]) -> bool:
        expected = to_int(batch.get("expected_count"))
        got_r = to_int(batch.get("receipt_count"))
        got_n = to_int(batch.get("record_count"))
        if expected > 0:
            return got_r >= expected and got_n >= expected
        # 开口区间：有显式完整标记且至少收到过回执/记录
        return bool(batch.get("complete_flag")) and (got_r > 0 or got_n > 0)

    def add_receipts(self, job: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any]:
        batch = self._resolve_batch(job, params)
        code = str((job or {}).get("源系统") or params.get("system") or params.get("源系统")
                   or (batch or {}).get("system") or "").strip()

        raw_list = params.get("receipts") or params.get("完成回执") or params.get("items")
        if isinstance(raw_list, dict):
            raw_list = [raw_list]
        count = to_int(params.get("count", params.get("条数", params.get("n"))), 0)
        entries: list[dict[str, Any]] = list(raw_list) if isinstance(raw_list, list) else []
        if not entries and count <= 0:
            records = params.get("records", params.get("record_count", params.get("记录数")))
            entries.append({"seq": params.get("seq", params.get("序号", 1)),
                            "records": records if records is not None else 1})
        while len(entries) < count:
            entries.append({"seq": len(entries) + 1, "records": 1})

        # 只“启动”未显式领取区间就上报回执：接入有效时按确认断点隐式建一个开口批次
        if batch is None and job is not None and self.is_access_open(code):
            created = self.claim_range(job, {"start": self.checkpoint(code), "count": 0})
            if created.get("ok"):
                batch = created["batch"]

        if batch is None:
            # 停用后且找不到在途批次：作为无主迟到回执挂到停用单上留痕
            receipt_ref = self.latest_receipt(code) if code else None
            if receipt_ref is not None and entries:
                note = [{"seq": e.get("seq"), "records": to_int(e.get("records"), 1),
                         "received_at": now_iso(), "accepted": False,
                         "reason": "找不到对应在途批次的迟到回执"} for e in entries if isinstance(e, dict)]
                receipt_ref.setdefault("late_receipts", []).extend(note)
                s = receipt_ref.setdefault("迟到回执核对", {"total": 0, "accepted": 0, "rejected": 0})
                s["total"] = to_int(s.get("total")) + len(note)
                s["rejected"] = to_int(s.get("rejected")) + len(note)
                self._persist()
            return {"ok": True, "recorded": len(entries), "accepted": 0, "rejected": len(entries),
                    "receipts": [], "disposal_receipt": receipt_ref, "receipt": receipt_ref,
                    "message": "找不到对应抽取批次；迟到回执已核对留痕，不改变确认断点"}

        code = code or str(batch.get("system", ""))
        receipt_ref = self.latest_receipt(code) if code else None
        after_stop = (not self.is_access_open(code)) if code else False

        recorded: list[dict[str, Any]] = []
        accepted_n = rejected_n = 0
        with self.lock:
            for idx, item in enumerate(entries, start=to_int(batch.get("receipt_count")) + 1):
                if not isinstance(item, dict):
                    item = {"records": item}
                rec_count = max(1, to_int(item.get("records", item.get("record_count", item.get("记录数"))), 1))
                seq_no = item.get("seq", item.get("序号", idx))
                late = bool(after_stop or item.get("late") or item.get("迟到"))
                rec = {
                    "receipt_no": f"CR-{batch['batch_no']}-{idx:02d}",
                    "batch_no": batch["batch_no"],
                    "job_id": batch.get("job_id"),
                    "seq": seq_no,
                    "records": rec_count,
                    "received_at": now_iso(),
                    "late": late,
                    "accepted": False,
                }
                if batch.get("terminated"):
                    rec["accepted"] = False
                    rec["reason"] = "原提交已终止，迟到回执仅核对留痕，不能确认区间"
                    rejected_n += 1
                elif not self.is_access_open(code):
                    # 停用交接中：先留痕，是否接受取决于处置结论
                    rec["accepted"] = False
                    rec["pending_disposition"] = True
                    rec["reason"] = "停用交接期间回执迟到，等待停用处置结论"
                    rejected_n += 1
                else:
                    rec["accepted"] = True
                    accepted_n += 1
                    batch["receipt_count"] = to_int(batch.get("receipt_count")) + 1
                    batch["record_count"] = to_int(batch.get("record_count")) + rec_count
                batch.setdefault("receipts", []).append(rec)
                recorded.append(rec)

            # 若处置单选择了接纳且回执已补齐，直接确认
            if not batch.get("terminated") and receipt_ref and \
                    parse_decision(receipt_ref.get("decision")) == DECISION_ACCEPT \
                    and self._batch_complete(batch) and not batch.get("confirmed"):
                batch["submitted"] = True
                self._confirm_batch(batch, by="late-receipts-accepted")

            if receipt_ref is not None:
                late_summary = receipt_ref.setdefault("迟到回执核对",
                                                      {"total": 0, "accepted": 0, "rejected": 0})
                late_summary["total"] = to_int(late_summary.get("total")) + len(recorded)
                late_summary["accepted"] = to_int(late_summary.get("accepted")) + accepted_n
                late_summary["rejected"] = to_int(late_summary.get("rejected")) + rejected_n
                receipt_ref["late_receipts"] = (receipt_ref.get("late_receipts") or []) + recorded

            complete_now = self._batch_complete(batch)
            if complete_now and not batch.get("submitted"):
                batch["state"] = "reading_complete_pending_submit"
                batch["状态"] = "回执已齐，待提交完整区间"
            elif not complete_now:
                batch["state"] = "reading"
                batch["状态"] = "半段读入（区间未完整，不可清洗）"
            self._persist()

        cp = self.checkpoint(code) if code else 0
        if batch.get("terminated"):
            message = (f"{len(recorded)} 条完成回执迟到：原提交已终止，回执已与停用处置回执 "
                       f"{receipt_ref['id'] if receipt_ref else ''} 核对留痕但不予确认；"
                       f"确认断点保持 {cp}，批次 {batch['batch_no']} 保留，恢复接入后可从 {cp} 接续")
            ok = True  # 回执登记成功；是否被接受看 accepted 字段
        elif rejected_n and not accepted_n:
            message = f"{len(recorded)} 条回执在停用交接期间迟到，已登记并等待处置结论，暂不构成完整区间"
            ok = True
        else:
            message = f"已登记 {accepted_n} 条完成回执；完整区间提交并确认后才可派发清洗"
            ok = True
        return {"ok": ok, "recorded": len(recorded), "accepted": accepted_n,
                "rejected": rejected_n, "receipts": recorded, "batch": batch,
                "checkpoint": cp, "确认断点": cp,
                "disposal_receipt": receipt_ref, "receipt": receipt_ref,
                "message": message}

    def submit_range(self, job: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any]:
        batch = self._resolve_batch(job, params)
        if batch is None:
            return {"ok": False, "message": "找不到对应抽取批次，无法提交区间"}
        code = str(batch.get("system", ""))
        denied = self.access_denied(code, to_int((job or {}).get("id")))
        if denied is not None and not params.get("allow_terminated_commit"):
            # 停用后提交：若批次是停用前的在途批次，按处置结论处理而不是笼统拒绝
            if batch.get("terminated"):
                denied["message"] = (f"批次 {batch['batch_no']} 的原提交已按停用处置终止，"
                                     f"不能再提交；请恢复接入后从确认断点 {self.checkpoint(code)} 接续")
                denied["batch"] = batch
                return denied
            return denied

        with self.lock:
            batch["submitted"] = True
            if params.get("complete") is True or str(params.get("complete", "")).lower() == "true":
                batch["complete_flag"] = True
            if params.get("record_count") is not None:
                batch["record_count"] = max(to_int(batch.get("record_count")),
                                            to_int(params.get("record_count")))
                if to_int(batch.get("expected_count")) == 0:
                    batch["expected_count"] = to_int(params.get("record_count"))
            # 开口区间（未预先声明宽度）在显式提交时视为客户端声明读完整段，
            # 右端按实际记录数补齐；预先声明宽度却没读满的仍是半段。
            if to_int(batch.get("expected_count")) == 0 and to_int(batch.get("receipt_count")) > 0:
                total = max(to_int(batch.get("record_count")), to_int(batch.get("receipt_count")))
                batch["expected_count"] = total
                batch["record_count"] = max(to_int(batch.get("record_count")), total)
                batch["range_end"] = to_int(batch.get("range_start")) + total
                batch["抽取范围"] = f"{batch['range_start']}-{batch['range_end']}"
                batch["记录范围"] = f"[{batch['range_start']}, {batch['range_end']})"
                batch["complete_flag"] = True
            if not self._batch_complete(batch):
                batch["complete"] = False
                batch["confirmed"] = False
                batch["consumable"] = False
                batch["state"] = "half_submitted"
                batch["状态"] = "半段提交（不是完整抽取区间，不可清洗）"
                self._persist()
                return {"ok": True, "confirmed": False, "consumable": False, "batch": batch,
                        "message": "只提交了半段区间/回执未覆盖完整范围，不算完成，不发清洗资格"}
            self._confirm_batch(batch, by="full-range-submit")
            self._persist()
        return {"ok": True, "confirmed": True, "consumable": True, "batch": batch,
                "checkpoint": self.checkpoint(code),
                "message": f"完整抽取区间 {batch['记录范围']} 已提交并经服务端确认，批次可消费、可派发清洗"}

    def complete_job(self, job: dict[str, Any], record_count: int | None = None,
                    params: dict[str, Any] | None = None) -> dict[str, Any]:
        """兼容既有 complete_and_advance 锚点：直接完成一个完整区间并推进断点。"""
        params = dict(params or {})
        code = str(job.get("源系统", "")).strip()
        denied = self.access_denied(code, to_int(job.get("id")))
        if denied is not None:
            return denied
        current = to_int(job.get("游标位置"), self.checkpoint(code))
        count = to_int(record_count if record_count is not None else params.get("record_count"), 0)
        with self.lock:
            batch = self._active_batch_for_job(to_int(job.get("id")))
            if batch is None:
                claim_params = {"start": current, "count": max(count, 1)}
                created = self.claim_range(job, claim_params)
                if not created.get("ok"):
                    return created
                batch = created["batch"]
            if count:
                batch["expected_count"] = count
                batch["receipt_count"] = max(to_int(batch.get("receipt_count")), count)
                batch["record_count"] = max(to_int(batch.get("record_count")), count)
            batch["submitted"] = True
            batch["complete_flag"] = True
            if not self._batch_complete(batch) and count:
                batch["expected_count"] = count
            self._confirm_batch(batch, by="complete_job")
            self._persist()
        return {"ok": True, "records": count, "batch": batch,
                "cursor": str(batch["range_end"]), "checkpoint": batch["range_end"],
                "message": f"作业完成，确认断点推进到 {batch['range_end']}，完整批次可派发清洗"}

    def _confirm_batch(self, batch: dict[str, Any], *, by: str) -> None:
        batch["submitted"] = True
        batch["complete"] = True
        batch["confirmed"] = True
        batch["consumable"] = True
        batch["terminated"] = False
        batch["state"] = "confirmed_consumable"
        batch["状态"] = "服务端已确认·可消费"
        batch["confirmed_at"] = now_iso()
        batch["confirmed_by"] = by
        code = str(batch.get("system", ""))
        end_v = to_int(batch.get("range_end"))
        if code:
            self.save_checkpoint(code, end_v, batch.get("entity") or None)
        job = store.find("record_extract", to_int(batch.get("job_id")))
        if job is not None:
            job["status"] = "已完成"
            job["pending"] = False
            job["abnormal"] = False
            job["游标位置"] = str(end_v)
            job["handoff_state"] = "confirmed_consumable"
        # 派发给已启用的匹配清洗规则（保留既有 dispatch_cleanse 联动）
        self._dispatch_to_rules(batch)

    def _dispatch_to_rules(self, batch: dict[str, Any]) -> None:
        rules = store.rows("std_cleanse")
        matched = [r for r in rules if str(r.get("status")) == "已启用"
                   and (not batch.get("entity") or str(r.get("实体编码")) == str(batch.get("entity")))]
        targets = matched or [r for r in rules if str(r.get("status")) == "已启用"]
        for rule in targets:
            rule.setdefault("待清洗批次", [])
            if batch["batch_no"] not in rule["待清洗批次"]:
                rule["待清洗批次"].append(batch["batch_no"])
            rule.setdefault("可消费批次", [])
            snapshot = {"batch_no": batch["batch_no"], "记录范围": batch["记录范围"],
                        "源系统": batch.get("源系统"), "实体编码": batch.get("实体编码")}
            if snapshot not in rule["可消费批次"]:
                rule["可消费批次"].append(snapshot)
            self._seq["dispatch"] += 1
            self.dispatches.append({
                "id": self._seq["dispatch"],
                "batch_no": batch["batch_no"],
                "batch_id": batch.get("id"),
                "rule_id": rule.get("id"),
                "rule_name": rule.get("规则名称", ""),
                "entity": batch.get("entity", ""),
                "range": batch["记录范围"],
                "dispatched_at": now_iso(),
                "status": "待领取",
            })

    def terminate_submission(self, job: dict[str, Any], reason: str = "") -> dict[str, Any]:
        """显式终止某个作业的在途/最近未确认提交；批次保留为可接续。"""
        job_id = to_int(job.get("id"))
        batch = self._active_batch_for_job(job_id) or self.latest_batch_for_job(job_id)
        code = str(job.get("源系统", ""))
        if batch is None:
            return {"ok": False, "message": "该作业没有可终止的抽取批次"}
        if batch.get("confirmed"):
            return {"ok": False, "message": "批次已经服务端确认，不能终止", "batch": batch}
        with self.lock:
            code = str(job.get("源系统", ""))
            resume_at = self.checkpoint(code)
            batch["terminated"] = True
            batch["state"] = "terminated_resumable"
            batch["状态"] = "处置终止：原提交已终止·批次保留可接续"
            batch["terminate_reason"] = reason or "按停用处置结论终止原提交"
            batch["resume_from"] = resume_at
            job["status"] = "抽取失败"
            job["abnormal"] = True
            job["handoff_state"] = "terminated_resumable"
            job["处置结果"] = f"原提交已终止，保留可接续批次，自断点 {resume_at} 接续"
            rcpt = self._stored_receipt_for(code)
            if rcpt is not None:
                for item in rcpt.get("jobs", []):
                    if to_int(item.get("job_id")) == job_id:
                        item.update({"state": "terminated_resumable",
                                     "result": job["处置结果"],
                                     "resume_from": resume_at})
            self._persist()
        return {"ok": True, "terminated": True, "batch": batch,
                "checkpoint": resume_at, "确认断点": resume_at,
                "message": job["处置结果"]}

    def accept_submission(self, job: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        """按“接纳”处置结论，把回执已齐的批次确认成完整区间（可消费）。"""
        params = params or {}
        job_id = to_int(job.get("id"))
        batch = self._active_batch_for_job(job_id) or self.latest_batch_for_job(job_id)
        if batch is None:
            return {"ok": False, "message": "该作业没有可确认的抽取批次"}
        with self.lock:
            if batch.get("terminated"):
                # 处置结论为接纳：撤销终止标记
                batch["terminated"] = False
            batch["submitted"] = True
            if params.get("record_count") is not None:
                batch["record_count"] = max(to_int(batch.get("record_count")), to_int(params.get("record_count")))
                if to_int(batch.get("expected_count")) == 0:
                    batch["expected_count"] = to_int(params.get("record_count"))
            if to_int(batch.get("expected_count")) == 0 and to_int(batch.get("receipt_count")) > 0:
                total = max(to_int(batch.get("record_count")), to_int(batch.get("receipt_count")))
                batch["expected_count"] = total
                batch["record_count"] = max(to_int(batch.get("record_count")), total)
                batch["range_end"] = to_int(batch.get("range_start")) + total
                batch["记录范围"] = f"[{batch['range_start']}, {batch['range_end']})"
            if not self._batch_complete(batch):
                batch["state"] = "half_submitted"
                batch["状态"] = "半段提交（回执未覆盖完整区间），不能确认"
                self._persist()
                return {"ok": False, "confirmed": False, "consumable": False, "batch": batch,
                        "message": "回执未覆盖完整抽取区间，按接纳处置仍不能确认；半段读入不算完成"}
            self._confirm_batch(batch, by="disposition-accept-explicit")
            self._persist()
        return {"ok": True, "confirmed": True, "consumable": True, "batch": batch,
                "checkpoint": self.checkpoint(str(job.get("源系统", ""))),
                "message": "按处置结论接纳并确认完整区间，批次可消费"}

    def resolve_receipt(self, code_or_rid: str, decision: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """对一张停用单的全部未确认在途批次给出统一处置（终止 / 接纳）。"""
        params = params or {}
        rid = str(code_or_rid or "")
        receipt = self.receipts.get(rid) or self._stored_receipt_for(rid) or self.latest_receipt(rid)
        if receipt is None:
            return {"ok": False, "message": f"找不到停用处置回执/源系统 {rid}"}
        code = str(receipt.get("system"))
        chosen = parse_decision(decision or params.get("decision") or params.get("disposition"),
                                DECISION_TERMINATE)
        results = []
        for job in store.rows("record_extract"):
            if str(job.get("源系统")) != code:
                continue
            batch = self._active_batch_for_job(to_int(job.get("id")))
            if batch is None:
                continue
            if chosen == DECISION_ACCEPT:
                results.append(self.accept_submission(job, params))
            else:
                results.append(self.terminate_submission(job, str(params.get("reason") or "")))
        receipt["decision"] = chosen
        receipt["处置结论"] = "终止原提交并保留可接续批次" if chosen == DECISION_TERMINATE else "接纳在途回执并核对确认"
        self._persist()
        return {"ok": True, "receipt": receipt, "decision": chosen, "results": results,
                "message": f"停用单 {receipt['id']} 已按「{receipt['处置结论']}」处置 {len(results)} 个在途批次"}

    def continue_job(self, job: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        code = str(job.get("源系统", "")).strip()
        denied = self.access_denied(code, to_int(job.get("id")))
        if denied is not None:
            denied["message"] = f"源系统「{code}」仍处于停用/关闭接入状态，尚不能接续；请先恢复接入。"
            return denied
        cp = self.checkpoint(code, str(job.get("实体编码") or "") or None)
        job["status"] = "执行中"
        job["pending"] = True
        job["abnormal"] = False
        job["游标位置"] = str(cp)
        job["handoff_state"] = "continued_from_checkpoint"
        job["处置结果"] = f"恢复接入，从确认断点 {cp} 接续"
        # 接续即领取断点后的新区间（默认 4 条宽度，可由参数覆盖）
        claim_params = {"start": cp}
        if params.get("end") is not None:
            claim_params["end"] = params["end"]
        if params.get("count") is not None:
            claim_params["count"] = params["count"]
        batch = self.claim_range(job, claim_params)
        if batch.get("ok"):
            batch["continued"] = True
            batch["message"] = f"原作业行继续，从确认断点 {cp} 接续新批次 {batch['batch_no']}"
        return batch

    # ------------------------------------------------------------------
    # 清洗侧：只列服务端确认可消费的批次 + 实领记录
    # ------------------------------------------------------------------
    def consumable_batches(self, system: str | None = None, entity: str | None = None) -> list[dict[str, Any]]:
        out = []
        for key in self.batch_order:
            b = self.batches[key]
            if not (b.get("confirmed") and b.get("submitted") and b.get("complete")
                    and b.get("consumable") and not b.get("terminated")):
                continue
            if system and b.get("system") != system:
                continue
            if entity and b.get("entity") and b.get("entity") != entity:
                continue
            out.append({k: v for k, v in b.items() if k != "receipts"})
        return out

    def pending_batches(self, system: str | None = None) -> list[dict[str, Any]]:
        """停用中的候补/半段/被终止区间：显式排除在清洗资格之外。"""
        out = []
        for key in self.batch_order:
            b = self.batches[key]
            if b.get("confirmed") and b.get("consumable"):
                continue
            if system and b.get("system") != system:
                continue
            out.append({k: v for k, v in b.items() if k != "receipts"})
        return out

    def claim_cleanse(self, params: dict[str, Any]) -> dict[str, Any]:
        batch = self._resolve_batch(None, params)
        if batch is None:
            return {"ok": False, "message": "找不到对应抽取批次"}
        if not (batch.get("confirmed") and batch.get("consumable") and not batch.get("terminated")):
            return {"ok": False, "eligible": False, "consumable": False, "batch": batch,
                    "message": f"批次 {batch['batch_no']} 尚未经服务端确认成完整抽取区间，不能获得清洗资格"}
        with self.lock:
            self._seq["claim"] += 1
            cid = self._seq["claim"]
            claim = {
                "id": cid,
                "claim_no": f"CL-{cid:04d}",
                "领取编号": f"CL-{cid:04d}",
                "batch_id": batch.get("id"),
                "batch_no": batch["batch_no"],
                "批次号": batch["batch_no"],
                "job_id": batch.get("job_id"),
                "源系统": batch.get("源系统"),
                "system": batch.get("system"),
                "实体编码": batch.get("实体编码"),
                "entity": batch.get("entity"),
                "记录范围": batch["记录范围"],
                "range": batch["记录范围"],
                "record_count": batch.get("record_count", batch.get("expected_count")),
                "实领记录数": batch.get("record_count", batch.get("expected_count")),
                "data": params.get("data") or batch.get("data") or [],
                "claimed_at": now_iso(),
                "status": "已实领",
            }
            self.claims[claim["claim_no"]] = claim
            self.claim_order.append(claim["claim_no"])
            batch.setdefault("cleanse_claims", []).append(claim["claim_no"])
            rule_id = params.get("rule_id") or params.get("entry_id")
            if rule_id is not None:
                rule = store.find("std_cleanse", to_int(rule_id))
                if rule is not None:
                    rule.setdefault("清洗实领数据", []).append({
                        "claim_no": claim["claim_no"], "batch_no": batch["batch_no"],
                        "记录范围": batch["记录范围"], "record_count": claim["record_count"]})
            self._persist()
        return {"ok": True, "claim": claim, "message": f"清洗已实领批次 {batch['batch_no']} {batch['记录范围']}"}

    # ------------------------------------------------------------------
    # 失败恢复：按确认批次/显式断点/游标重算，绝不把候补区间算成确认位置
    # ------------------------------------------------------------------
    def recover(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        with self.lock:
            before = dict(self.confirmed_positions)
            # 只恢复“交接链路真正管理过”的源系统，不碰无关 seed 样例行
            systems: set[str] = set(self.confirmed_positions)
            systems.update(str(b.get("system")) for b in self.batches.values() if b.get("system"))
            systems.update(str(r.get("system")) for r in self.receipts.values() if r.get("system"))
            systems.update(str(k) for k in self.system_access)
            checkpoints: dict[str, int] = {}
            for code in systems:
                if not code:
                    continue
                # 只由“已提交且完整且确认、未终止”的批次推进
                cp = max(int(before.get(code, 0)), self.cursor_value(code))
                for b in self.batches.values():
                    if b.get("system") == code and b.get("confirmed") \
                            and b.get("submitted") and b.get("complete") and not b.get("terminated"):
                        cp = max(cp, to_int(b.get("range_end")))
                    if b.get("terminated") and b.get("resume_from") is not None \
                            and b.get("system") == code:
                        # 被终止批次记录的接续点本身就是旧确认位，不贡献新区间
                        cp = max(cp, to_int(b.get("resume_from")))
                self.confirmed_positions[code] = cp
                checkpoints[code] = cp
                # 仅校正本链路管理的游标行；非数字样例值不做破坏性改写
                for cur in store.rows("sync_cursor"):
                    if str(cur.get("源系统")) != code:
                        continue
                    if to_int(cur.get("游标值"), -1) >= 0 or str(cur.get("游标值", "")).isdigit():
                        cur["游标值"] = str(cp)
            # 校正作业行状态
            for job in store.rows("record_extract"):
                b = self.latest_batch_for_job(to_int(job.get("id")))
                if b is None:
                    continue
                if b.get("confirmed"):
                    job["status"] = "已完成"
                    job["pending"] = False
                elif b.get("terminated"):
                    job["status"] = "抽取失败"
                    job["handoff_state"] = "terminated_resumable"
                    job["处置结果"] = f"恢复核对：原提交已终止，可自 {checkpoints.get(str(job.get('源系统')), 0)} 接续"
                job["游标位置"] = str(checkpoints.get(str(job.get("源系统")),
                                                      to_int(job.get("游标位置"))))
            self._persist()
        report = {
            "ok": True,
            "recovered": True,
            "message": "服务端失败恢复完成：已按确认批次与游标重算断点，已确认位置未丢失",
            "checkpoints": checkpoints,
            "确认断点": checkpoints,
            "before": before,
            "receipts": len(self.receipt_order),
            "batches": len(self.batch_order),
            "consumable_batches": len(self.consumable_batches()),
            "claims": len(self.claim_order),
            "dispatches": len(self.dispatches),
        }
        return report

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "receipts": [self.receipts[r] for r in self.receipt_order],
            "batches": [self.batches[b] for b in self.batch_order],
            "consumable_batches": self.consumable_batches(),
            "pending_batches": self.pending_batches(),
            "claims": [self.claims[c] for c in self.claim_order],
            "dispatches": self.dispatches,
            "checkpoints": {code: self.checkpoint(code) for code in
                            set(list(self.confirmed_positions)
                                + [str(s.get("系统标识")) for s in store.rows("source_system")])
                            if code},
            "access": {str(s.get("系统标识")): self.is_access_open(str(s.get("系统标识")))
                       for s in store.rows("source_system") if s.get("系统标识")},
        }

    # ------------------------------------------------------------------
    # 一键演练场景：CRM/ERP + 确认断点 800 + 已派清洗批次
    # ------------------------------------------------------------------
    def seed_crm_erp_scenario(self) -> dict[str, Any]:
        created: dict[str, Any] = {"systems": {}, "jobs": {}}

        def find_or_create_system(code: str, name: str) -> dict[str, Any]:
            row = self.source_row(code)
            if row is not None:
                return row
            rows = store.rows("source_system")
            sid = max((to_int(r.get("id")) for r in rows), default=0) + 1
            row = {"id": sid, "系统标识": code, "系统名称": name, "数据域": "客户数据" if code == "CRM" else "企业资源",
                   "同步频率": "实时", "责任人": "数据工程组", "status": "已接入",
                   "pending": False, "abnormal": False}
            rows.append(row)
            return row

        crm = find_or_create_system("CRM", "CRM 客户源系统")
        erp = find_or_create_system("ERP", "ERP 订单源系统")
        created["systems"] = {"CRM": crm.get("id"), "ERP": erp.get("id")}

        # CRM 已确认断点 800；ERP 独立断点 1200
        self.save_checkpoint("CRM", 800)
        self.save_checkpoint("ERP", 1200)

        def ensure_job(name: str, code: str, entity: str) -> dict[str, Any]:
            jobs = store.rows("record_extract")
            job = next((j for j in jobs if str(j.get("作业名称")) == name), None)
            if job is None:
                jid = max((to_int(j.get("id")) for j in jobs), default=0) + 1
                job = {"id": jid, "作业名称": name, "源系统": code, "实体编码": entity,
                       "抽取范围": "0-800", "游标位置": "800", "status": "待执行",
                       "pending": True, "abnormal": False}
                jobs.append(job)
            return job

        crm_job = ensure_job("CRM-客户记录抽取-800后续", "CRM", "CUSTOMER")
        erp_job = ensure_job("ERP-订单断点作业", "ERP", "ORDER")
        created["jobs"] = {"CRM": crm_job.get("id"), "ERP": erp_job.get("id")}

        # 一个已经完整确认、已派给清洗且被实领的历史批次 [796,800)
        if not any(b.get("system") == "CRM" and b.get("range_end") == 800
                   for b in self.batches.values()):
            old = self.claim_range(crm_job, {"start": 796, "end": 800})
            b = old["batch"]
            b["receipt_count"] = 4
            b["record_count"] = 4
            b["expected_count"] = 4
            self._confirm_batch(b, by="scenario-seed")
            claim = self.claim_cleanse({"batch_id": b["batch_no"]})
            created["historical_batch"] = b["batch_no"]
            created["historical_claim"] = claim.get("claim", {}).get("claim_no")

        # 当前作业回到“待领取 800 后区间”的干净演练起点
        crm_job["status"] = "待执行"
        crm_job["pending"] = True
        crm_job["游标位置"] = "800"
        self._persist()
        return {"ok": True, **created,
                "checkpoints": {"CRM": 800, "ERP": 1200},
                "message": "已准备 CRM/ERP 演练：CRM 确认断点 800，ERP 断点 1200，另有已清洗实领的完整历史批次"}


handoff = HandoffCore()
