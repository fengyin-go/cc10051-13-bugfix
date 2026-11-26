"""cc10051 交接链路的语义接口。

通用 /api/{module}/{id}/actions 已能覆盖大部分操作；这里再提供一组直观的
REST 路径与别名，方便验收脚本按“停用单 / 回执 / 批次 / 断点 / 实领”直接核对。
所有路径都只是 app.handoff 单例的薄封装，不另立状态。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.handoff import handoff
from app.store import store

router = APIRouter(prefix="/api", tags=["源系统交接链路"])


async def body_of(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {"raw": data}


def _job(job_id: int) -> dict[str, Any] | None:
    return store.find("record_extract", job_id)


# ---------------------------------------------------------------------------
# 总览 / 恢复 / 复位 / 演练场景
# ---------------------------------------------------------------------------
@router.get("/handoff/state")
def handoff_state() -> dict[str, Any]:
    return {"ok": True, **handoff.state_snapshot()}


@router.post("/handoff/recover")
async def handoff_recover(request: Request) -> dict[str, Any]:
    return handoff.recover(await body_of(request))


@router.post("/handoff/reset")
async def handoff_reset(request: Request) -> dict[str, Any]:
    return handoff.reset()


@router.post("/handoff/scenarios/crm-erp-800")
async def seed_scenario(request: Request) -> dict[str, Any]:
    return handoff.seed_crm_erp_scenario()


# 直观别名：恢复 / 场景准备可能被放在不同前缀下调用
for _path in ("/recover", "/recovery", "/handoff/recovery", "/record_extract/recover",
              "/record_extract/recovery", "/source_system/recover"):
    router.add_api_route(_path, handoff_recover, methods=["POST"])

for _path in ("/scenario/crm-erp-800", "/handoff/seed", "/handoff/scenario",
              "/source_system/seed-crm-scenario"):
    router.add_api_route(_path, seed_scenario, methods=["POST"])


# ---------------------------------------------------------------------------
# 停用处置回执
# ---------------------------------------------------------------------------
@router.get("/handoff/receipts")
def list_receipts(system: str | None = None) -> dict[str, Any]:
    items = [handoff.receipts[r] for r in handoff.receipt_order]
    if system:
        items = [r for r in items if r.get("system") == system or system in (r.get("scope_systems") or [])]
    return {"items": items, "total": len(items), "ok": True}


@router.get("/handoff/receipts/{receipt_id}")
def get_receipt(receipt_id: str) -> dict[str, Any]:
    receipt = handoff.receipts.get(receipt_id)
    if receipt is None:
        for r in handoff.receipts.values():
            if r.get("receipt_no") == receipt_id or r.get("id") == receipt_id:
                receipt = r
                break
    if receipt is None:
        return {"ok": False, "message": f"停用处置回执 {receipt_id} 不存在"}
    return {"ok": True, "receipt": receipt}


# ---------------------------------------------------------------------------
# 断点
# ---------------------------------------------------------------------------
@router.get("/handoff/checkpoints")
def list_checkpoints() -> dict[str, Any]:
    snap = handoff.state_snapshot()
    return {"ok": True, "items": [
        {"源系统": code, "checkpoint": value, "确认断点": value, "游标值": str(value)}
        for code, value in snap["checkpoints"].items()
    ], "checkpoints": snap["checkpoints"]}


@router.get("/handoff/checkpoints/{code}")
def get_checkpoint(code: str) -> dict[str, Any]:
    value = handoff.checkpoint(code)
    return {"ok": True, "源系统": code, "checkpoint": value, "确认断点": value, "游标值": str(value)}


@router.post("/handoff/checkpoints/{code}")
async def set_checkpoint(code: str, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    value = data.get("value", data.get("checkpoint", data.get("确认断点", data.get("游标值"))))
    from app.handoff import to_int
    cp = handoff.save_checkpoint(code, to_int(value, handoff.checkpoint(code)),
                                 data.get("entity") or data.get("实体编码"))
    return {"ok": True, "源系统": code, "checkpoint": cp, "确认断点": cp}


# ---------------------------------------------------------------------------
# 批次 / 可消费清单 / 候补清单
# ---------------------------------------------------------------------------
@router.get("/handoff/batches")
def list_batches(system: str | None = None) -> dict[str, Any]:
    items = [handoff.batches[b] for b in handoff.batch_order]
    if system:
        items = [b for b in items if b.get("system") == system]
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/handoff/consumable-batches")
def consumable_batches(system: str | None = None, entity: str | None = None) -> dict[str, Any]:
    items = handoff.consumable_batches(system, entity)
    return {"ok": True, "items": items, "total": len(items),
            "message": "仅列出服务端确认为完整提交区间、可消费的抽取批次"}


@router.get("/handoff/pending-batches")
def pending_batches(system: str | None = None) -> dict[str, Any]:
    items = handoff.pending_batches(system)
    return {"ok": True, "items": items, "total": len(items),
            "message": "半段读入/停用候补/被终止区间，不计入清洗资格"}


@router.post("/handoff/batches/{ref}/claim-cleanse")
async def claim_batch(ref: str, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    data["batch_id"] = ref
    return handoff.claim_cleanse(data)


# ---------------------------------------------------------------------------
# 清洗实领记录 / 派发记录
# ---------------------------------------------------------------------------
@router.get("/handoff/claims")
def list_claims() -> dict[str, Any]:
    items = [handoff.claims[c] for c in handoff.claim_order]
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/handoff/dispatches")
def list_dispatches() -> dict[str, Any]:
    return {"ok": True, "items": handoff.dispatches, "total": len(handoff.dispatches)}


@router.post("/handoff/receipts/{ref}/resolve")
async def resolve_receipt(ref: str, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    decision = str(data.get("decision") or data.get("disposition")
                   or data.get("处置结论") or data.get("在途处置") or "")
    return handoff.resolve_receipt(ref, decision, data)


# ---------------------------------------------------------------------------
# 按源系统标识停用 / 接入
# ---------------------------------------------------------------------------
@router.post("/handoff/systems/{code}/deactivate")
async def deactivate_by_code(code: str, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    return handoff.deactivate(code, reason=str(data.get("reason") or data.get("原因") or ""),
                              params=data)


@router.post("/handoff/systems/{code}/reactivate")
async def reactivate_by_code(code: str, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    return handoff.reactivate(code, reason=str(data.get("reason") or ""))


# ---------------------------------------------------------------------------
# 按作业 id 的语义操作
# ---------------------------------------------------------------------------
@router.post("/handoff/jobs/{job_id}/start")
async def job_start(job_id: int, request: Request) -> dict[str, Any]:
    job = _job(job_id)
    if job is None:
        return {"ok": False, "message": "抽取作业不存在"}
    from app.services.record_extract import RecordExtractService
    return RecordExtractService().run_action_full(job_id, "启动作业", await body_of(request))


@router.post("/handoff/jobs/{job_id}/claim")
async def job_claim(job_id: int, request: Request) -> dict[str, Any]:
    job = _job(job_id)
    if job is None:
        return {"ok": False, "message": "抽取作业不存在"}
    return handoff.claim_range(job, await body_of(request))


@router.post("/handoff/jobs/{job_id}/receipts")
async def job_receipts(job_id: int, request: Request) -> dict[str, Any]:
    job = _job(job_id)
    if job is None:
        return {"ok": False, "message": "抽取作业不存在"}
    return handoff.add_receipts(job, await body_of(request))


@router.post("/handoff/jobs/{job_id}/submit")
async def job_submit(job_id: int, request: Request) -> dict[str, Any]:
    job = _job(job_id)
    if job is None:
        return {"ok": False, "message": "抽取作业不存在"}
    return handoff.submit_range(job, await body_of(request))


@router.post("/handoff/jobs/{job_id}/complete")
async def job_complete(job_id: int, request: Request) -> dict[str, Any]:
    job = _job(job_id)
    if job is None:
        return {"ok": False, "message": "抽取作业不存在"}
    data = await body_of(request)
    return handoff.complete_job(job, data.get("record_count") or data.get("count"), data)


@router.post("/handoff/jobs/{job_id}/continue")
async def job_continue(job_id: int, request: Request) -> dict[str, Any]:
    job = _job(job_id)
    if job is None:
        return {"ok": False, "message": "抽取作业不存在"}
    return handoff.continue_job(job, await body_of(request))


# ---------------------------------------------------------------------------
# 模块作用域别名：贴合通用资源路径与服务层锚点方法名
# ---------------------------------------------------------------------------
from app.services.record_extract import RecordExtractService  # noqa: E402

_extract_svc = RecordExtractService()

for _path in ("/record_extract/{job_id}/start",
              "/record_extract/{job_id}/start_with_cursor",
              "/record_extract/{job_id}/start-with-cursor"):
    router.add_api_route(_path, job_start, methods=["POST"])
for _path in ("/record_extract/{job_id}/claim",
              "/record_extract/{job_id}/claim-range"):
    router.add_api_route(_path, job_claim, methods=["POST"])
for _path in ("/record_extract/{job_id}/receipts",
              "/record_extract/{job_id}/complete-receipts"):
    router.add_api_route(_path, job_receipts, methods=["POST"])
for _path in ("/record_extract/{job_id}/submit",
              "/record_extract/{job_id}/submit-range"):
    router.add_api_route(_path, job_submit, methods=["POST"])
for _path in ("/record_extract/{job_id}/continue",
              "/record_extract/{job_id}/resume"):
    router.add_api_route(_path, job_continue, methods=["POST"])
for _path in ("/record_extract/{job_id}/complete",
              "/record_extract/{job_id}/complete_and_advance",
              "/record_extract/{job_id}/complete-and-advance"):
    router.add_api_route(_path, job_complete, methods=["POST"])


@router.post("/record_extract/{job_id}/fail_with_retry")
async def job_fail(job_id: int, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    return _extract_svc.fail_with_retry(job_id, str(data.get("reason") or data.get("原因") or "模拟失败"))


@router.post("/record_extract/{job_id}/dispatch_cleanse")
async def job_dispatch(job_id: int) -> dict[str, Any]:
    count = _extract_svc.dispatch_cleanse(job_id)
    return {"ok": count > 0, "dispatched_rules": count}


@router.get("/std_cleanse/consumable-batches")
def cleanse_consumable() -> dict[str, Any]:
    items = handoff.consumable_batches()
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/std_cleanse/claims")
def cleanse_claims_view() -> dict[str, Any]:
    items = [handoff.claims[c] for c in handoff.claim_order]
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/std_cleanse/batches/{ref}/claim")
async def cleanse_claim_by_ref(ref: str, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    data["batch_id"] = ref
    return handoff.claim_cleanse(data)


@router.post("/source_system/{source_id}/deactivate")
async def source_deactivate_scoped(source_id: int, request: Request) -> dict[str, Any]:
    from app.services.source_system import SourceSystemService
    data = await body_of(request)
    reason = str(data.get("reason") or data.get("原因") or "")
    return SourceSystemService().stop_with_receipt(source_id, reason, data)


@router.post("/source_system/{source_id}/stop_and_disconnect")
async def source_stop_anchor(source_id: int, request: Request) -> dict[str, Any]:
    data = await body_of(request)
    reason = str(data.get("reason") or data.get("原因") or data.get("remark") or "停用")
    from app.services.source_system import SourceSystemService
    text = SourceSystemService().stop_and_disconnect(source_id, reason)
    receipt = handoff.latest_receipt(str(store.find("source_system", source_id).get("系统标识", ""))) \
        if store.find("source_system", source_id) else None
    return {"ok": True, "message": text, "receipt": receipt, "receipt_id": (receipt or {}).get("id")}


@router.post("/source_system/{source_id}/reactivate")
async def source_reactivate_scoped(source_id: int, request: Request) -> dict[str, Any]:
    row = store.find("source_system", source_id)
    if row is None:
        return {"ok": False, "message": "源系统不存在"}
    return handoff.reactivate(row, reason=str((await body_of(request)).get("reason") or ""))
