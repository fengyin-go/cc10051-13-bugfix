"""标准化清洗领域服务：管理主数据记录的清洗规则、阶段编排、异常处置与检测派发。

数据链路位置：主数据加工层第一环，负责消费抽取作业产物，
把清洗后的数据集派发给重复检测与质量检查。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.handoff import handoff
from app.services.base import BaseModuleService
from app.store import store

CLAIM_BATCH_ACTIONS = {"领取批次", "领取清洗批次", "清洗实领", "实领批次", "领取清洗",
                       "claim_batch", "claim"}


class StdCleanseService(BaseModuleService):
    MODULE = "std_cleanse"
    ENTITY = "清洗规则"
    OPERATION_TERM = "清洗"
    LIST_FIELDS = ["规则名称", "实体编码", "清洗阶段", "规则表达式", "异常处理"]
    REQUIRED_FIELDS = ["规则名称", "实体编码", "清洗阶段"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def run_action_full(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = params or {}
        act = action.strip()
        if act in CLAIM_BATCH_ACTIONS:
            params = dict(params)
            params.setdefault("rule_id", entry_id)
            result = handoff.claim_cleanse(params)
            if result.get("ok"):
                result["entry"] = store.find(self.MODULE, entry_id)
            return result
        return super().run_action_full(entry_id, act, params)

    # ---- 清洗资格：只认服务端确认的完整抽取批次 ----
    def consumable_batches(self, entity_code: str | None = None) -> list[dict[str, Any]]:
        return handoff.consumable_batches(entity=entity_code or None)

    def actual_claims(self) -> list[dict[str, Any]]:
        return [handoff.claims[k] for k in handoff.claim_order]

    # ---- 业务扩展：标准化清洗与检测派发 ----

    def apply_rule(self, entry_id: int, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """按规则表达式清洗原始行，输出去空标准化后的记录。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"ok": False, "message": "清洗规则未启用", "cleaned": []}
        cleaned: list[dict[str, Any]] = []
        dropped = 0
        expr = str(entry.get("规则表达式", ""))
        for row in raw_rows:
            values = {k: str(v).strip() for k, v in row.items()}
            if not values.get("实体编码"):
                dropped += 1
                continue
            if "upper" in expr:
                values["实体编码"] = values["实体编码"].upper()
            cleaned.append(values)
        return {"ok": True, "cleaned": cleaned, "dropped": dropped}

    def enqueue_detection(self, entry_id: int) -> dict[str, Any]:
        """把清洗结果排入重复检测队列，写入 dup_detect。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清洗规则不存在"}
        batches = store.rows("dup_detect")
        batch_id = max((int(r.get("id", 0)) for r in batches), default=0) + 1
        batches.append({
            "id": batch_id,
            "检测批次": f"dup-{entry.get('实体编码')}-{batch_id}",
            "实体编码": entry.get("实体编码", ""),
            "候选组数": "0",
            "相似度算法": "编辑距离",
            "置信区间": "[0.85,1.0]",
            "status": "检测中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "batch_id": batch_id}

    def handle_anomaly(self, entry_id: int, reason: str) -> str:
        """处理清洗异常：按异常处理策略转人工或忽略。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "清洗规则不存在"
        strategy = str(entry.get("异常处理", "转人工"))
        if strategy == "转人工":
            reviews = store.rows("merge_review")
            review_id = max((int(r.get("id", 0)) for r in reviews), default=0) + 1
            reviews.append({
                "id": review_id,
                "记录编号": f"rv-{entry_id}-{review_id}",
                "候选组": "",
                "复核结论": "清洗异常",
                "复核角色": "数据管理员",
                "复核时间": datetime.now(timezone.utc).isoformat(),
                "status": "待复核",
                "pending": True,
                "abnormal": True,
            })
            return f"异常已转人工复核: {reason}"
        entry["abnormal"] = False
        return f"异常已按策略忽略: {reason}"

    def consume_extract_output(self, entry_id: int) -> list[str]:
        """读取抽取作业产物，聚合待清洗批次。

        cc10051：只返回服务端确认过的完整抽取批次；半段读入、停用候补区间不算。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        entity_code = entry.get("实体编码", "")
        names = [b.get("batch_no", "") for b in handoff.consumable_batches(entity=entity_code or None)]
        # 兼容旧数据：没有交接批次台账时，退回已完成作业名
        if not names:
            return [j.get("作业名称", "") for j in store.rows("record_extract")
                    if j.get("实体编码") == entity_code and j.get("status") == "已完成"]
        return names

    def produce_quality_input(self, entry_id: int, cleaned_count: int) -> int:
        """把清洗结果作为质量检查输入，写入 quality_result。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        results = store.rows("quality_result")
        report_id = max((int(r.get("id", 0)) for r in results), default=0) + 1
        results.append({
            "id": report_id,
            "报告批次": f"qr-{entry.get('实体编码')}-{report_id}",
            "实体编码": entry.get("实体编码", ""),
            "检查总数": str(cleaned_count),
            "命中数": "0",
            "评分结果": "pending",
            "status": "生成中",
            "pending": True,
            "abnormal": False,
        })
        return report_id

    def batch_enable(self, entity_code: str) -> int:
        """批量启用某实体下的全部草稿清洗规则。"""
        enabled = 0
        for rule in store.rows(self.MODULE):
            if rule.get("实体编码") == entity_code and rule.get("status") == "草稿":
                rule["status"] = "已启用"
                rule["pending"] = True
                enabled += 1
        return enabled
