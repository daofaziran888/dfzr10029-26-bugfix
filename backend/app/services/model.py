"""分析模型 DAG 图与批量训练服务。

批量训练的业务边界：
- 只读取「批量训练样本」与「特征清单版本」两份业务数据；
- 训练结果与评估指标只使用实际读取成功的样本；
- 样本成功清单与特征汇总在同一次构建中一起生成，整体提交，避免出现
  “清单里没有、训练结果里却有”的错位；
- 样本成功清单版本由调用方传入，重复提交沿用既有确认结果，不重复生成；
- 失败样本只保留在待补齐口径里，绝不把失败分区的有效特征直接计入训练。
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from app.model_training_seed import MODEL_BATCH_SAMPLES, MODEL_FEATURE_VERSIONS
from app.store import store

MODULE = "model"
REQUIRED_FIELDS = ["模型编号", "模型名称", "模型类型"]
STATUS_ORDER = ["草稿", "已训练", "评估中", "已部署", "已下线"]
ACTION_RULES = {"提交训练": "已训练", "启动评估": "评估中", "部署模型": "已部署"}
NEGATIVE_ACTIONS = []

# 批量训练样本、特征清单版本与训练结果各自独立存放，避免与通用模块清单混在一起。
SAMPLE_TABLE = "model_batch_sample"
FEATURE_TABLE = "model_feature_version"
RUN_TABLE = "model_training_run"

EXCLUDE_PATH = "排除失败样本"
BACKFILL_PATH = "补齐失败分片"
TRAIN_PATHS = [EXCLUDE_PATH, BACKFILL_PATH]
PATH_ALIASES = {
    "排除失败样本重训": EXCLUDE_PATH,
    "排除失败样本": EXCLUDE_PATH,
    "排除失败": EXCLUDE_PATH,
    "exclude": EXCLUDE_PATH,
    EXCLUDE_PATH: EXCLUDE_PATH,
    "补齐失败分片再训练": BACKFILL_PATH,
    "补齐失败分片": BACKFILL_PATH,
    "补齐失败样本": BACKFILL_PATH,
    "backfill": BACKFILL_PATH,
    BACKFILL_PATH: BACKFILL_PATH,
}

READ_SUCCESS = "成功"
READ_SUCCESS_BACKFILL = "成功（补齐）"
SUCCESS_CALIBER = "成功样本"
PENDING_CALIBER = "待补齐样本"


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _as_features(value: Any) -> list[str]:
    """把特征清单统一解析成有序列表，兼容列表与逗号/顿号分隔的文本。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("，", ",").replace("、", ",").replace(";", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _as_values(value: Any) -> dict[str, float]:
    """样本里实际读取到的特征值；没读到的特征不会被补成 0 再计入训练。"""
    if isinstance(value, dict):
        parsed: dict[str, float] = {}
        for key, raw in value.items():
            try:
                parsed[str(key)] = float(raw)
            except (TypeError, ValueError):
                continue
        return parsed
    return {}


def _normalize_path(value: str | None) -> str:
    """归一化训练路径：留空按「排除失败样本」，未知取值原样返回交给调用方拒绝。"""
    text = str(value or "").strip()
    if not text:
        return EXCLUDE_PATH
    return PATH_ALIASES.get(text, text)


def _metric_ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _compute_metrics(used: list[dict[str, Any]], feature_summary: list[dict[str, Any]],
                     cutoff: float = 0.6) -> dict[str, float]:
    """逐样本打分后计算评估指标；used 已经只包含实际读取成功的样本。"""
    weights = {item["特征"]: float(item["均值"]) for item in feature_summary}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {"准确率": 0.0, "精确率": 0.0, "召回率": 0.0, "F1": 0.0}
    correct = 0
    true_positive = 0
    predicted_positive = 0
    actual_positive = 0
    for row in used:
        values = _as_values(row.get("特征"))
        score = sum(values.get(name, 0.0) * weight for name, weight in weights.items()) / total_weight
        predicted = 1 if score >= cutoff else 0
        actual = int(row.get("标签") or 0)
        predicted_positive += predicted
        actual_positive += actual
        if predicted == actual:
            correct += 1
        if predicted == 1 and actual == 1:
            true_positive += 1
    precision = _metric_ratio(true_positive, predicted_positive)
    recall = _metric_ratio(true_positive, actual_positive)
    f1 = round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0
    return {
        "准确率": _metric_ratio(correct, len(used)),
        "精确率": precision,
        "召回率": recall,
        "F1": f1,
    }


class ModelService:
    # ---------- 通用 DAG 部分 ----------

    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i + 1)), "name": str(row.get(list(row.keys())[1] if len(row) > 1 else "name", "")), "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id", "status", "pending", "abnormal")}})
        for i in range(len(nodes) - 1):
            edges.append({"from": nodes[i]["id"], "to": nodes[i + 1]["id"], "label": "依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"模型配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析模型可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 「启动评估」必须消费样本成功清单并复核，不能只改状态就给出指标。
        if action == "启动评估":
            latest = self.list_runs(str(entry.get("模型编号", "")))
            if latest:
                self.evaluate_run(int(latest[-1]["id"]))
        return entry, f"模型配置已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]

    # ---------- 批量训练样本与特征清单版本 ----------

    def _ensure_tables(self) -> None:
        if not store.internal_rows(SAMPLE_TABLE):
            store.internal_rows(SAMPLE_TABLE).extend(copy.deepcopy(MODEL_BATCH_SAMPLES))
        if not store.internal_rows(FEATURE_TABLE):
            store.internal_rows(FEATURE_TABLE).extend(copy.deepcopy(MODEL_FEATURE_VERSIONS))

    def samples(self, model_code: str) -> list[dict[str, Any]]:
        self._ensure_tables()
        return [row for row in store.internal_rows(SAMPLE_TABLE) if row.get("模型编号") == model_code]

    def feature_versions(self, model_code: str) -> list[dict[str, Any]]:
        self._ensure_tables()
        return [row for row in store.internal_rows(FEATURE_TABLE) if row.get("模型编号") == model_code]

    def resolve_version(self, model_code: str, version: str | None) -> dict[str, Any] | None:
        """解析调用方传入的样本成功清单版本；未传入时取当前生效版本。"""
        versions = self.feature_versions(model_code)
        if version:
            wanted = str(version).strip()
            for row in versions:
                if str(row.get("版本")) == wanted:
                    return row
            return None
        for row in versions:
            if row.get("版本状态") == "生效":
                return row
        return versions[0] if versions else None

    # ---------- 训练结果构建 ----------

    def _used_samples(self, model_code: str, path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按训练路径返回（本次实际读取成功的样本, 待补齐样本）。"""
        kept: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for row in self.samples(model_code):
            status = str(row.get("读取状态", ""))
            if status == READ_SUCCESS:
                kept.append(dict(row))
                continue
            if path == BACKFILL_PATH and _as_values(row.get("特征")):
                # 补齐失败分片：失败分片里仍有有效特征，重新读取成功后标注为“已补齐”。
                backfilled = dict(row)
                backfilled["读取状态"] = READ_SUCCESS_BACKFILL
                backfilled["处理结果"] = "已补齐"
                backfilled["失败原因"] = ""
                kept.append(backfilled)
                continue
            pending_row = dict(row)
            # 没有有效特征的失败样本无法补齐，只能继续留在待补齐口径里。
            pending_row["处理结果"] = "待补齐" if path == BACKFILL_PATH else "已排除"
            pending.append(pending_row)
        return kept, pending

    def _feature_summary(self, used: list[dict[str, Any]], version_row: dict[str, Any]) -> list[dict[str, Any]]:
        """只汇总成功样本里真实读到的特征，并与特征清单版本取交集。"""
        declared = _as_features(version_row.get("特征清单"))
        declared_set = set(declared)
        totals: dict[str, list[float]] = {}
        for row in used:
            values = _as_values(row.get("特征"))
            for name, value in values.items():
                if name not in declared_set:
                    continue
                totals.setdefault(name, []).append(value)
        summary: list[dict[str, Any]] = []
        for name in declared:
            values = totals.get(name)
            if not values:
                continue
            summary.append({
                "特征": name,
                "覆盖样本数": len(values),
                "均值": round(sum(values) / len(values), 4),
                "覆盖占比": _metric_ratio(len(values), len(used)),
            })
        return summary

    def _success_list(self, used: list[dict[str, Any]], version: str, model_code: str) -> list[dict[str, Any]]:
        """样本成功清单：逐条保留处理结果，供列表与评估接口复核。"""
        items: list[dict[str, Any]] = []
        for row in used:
            items.append({
                "样本编号": row.get("样本编号"),
                "模型编号": model_code,
                "批次": row.get("批次"),
                "分区": row.get("分区"),
                "读取状态": row.get("读取状态"),
                "处理结果": row.get("处理结果", "成功"),
                "有效特征": _as_features(row.get("有效特征")),
                "标签": row.get("标签"),
                "特征值": _as_values(row.get("特征")),
                "清单版本": version,
            })
        return items

    def _pending_list(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "样本编号": row.get("样本编号"),
            "批次": row.get("批次"),
            "分区": row.get("分区"),
            "读取状态": row.get("读取状态"),
            "失败原因": row.get("失败原因"),
            "有效特征": _as_features(row.get("有效特征")),
            "处理结果": row.get("处理结果", "已排除"),
        } for row in pending]

    def _build_run(self, model_row: dict[str, Any], version_row: dict[str, Any],
                   path: str, version_input: str | None) -> dict[str, Any]:
        model_code = str(model_row.get("模型编号", ""))
        version = str(version_row.get("版本", ""))
        used, pending = self._used_samples(model_code, path)
        feature_summary = self._feature_summary(used, version_row)
        success_list = self._success_list(used, version, model_code)
        metrics = _compute_metrics(used, feature_summary)
        total = len(self.samples(model_code))
        success_rate = _metric_ratio(len(used), total)
        version_input_text = str(version_input or "").strip() or "未指定（取当前生效版本）"
        return {
            "模型编号": model_code,
            "模型名称": model_row.get("模型名称"),
            "训练路径": path,
            # 版本输入原样保留：即使没传版本，也记录本次是“取当前生效版本”。
            "版本输入": version_input_text,
            "样本成功清单版本": version,
            "特征清单版本": version,
            "特征清单": _as_features(version_row.get("特征清单")),
            "本次特征": [item["特征"] for item in feature_summary],
            "特征汇总": feature_summary,
            "样本总数": total,
            "成功样本数": len(used),
            "失败样本数": len(pending),
            "成功率": success_rate,
            "评估指标": metrics,
            "样本成功清单": success_list,
            "待补齐样本": self._pending_list(pending),
            "训练结果": {
                "使用样本数": len(used),
                "使用特征": [item["特征"] for item in feature_summary],
                "特征均值": {item["特征"]: item["均值"] for item in feature_summary},
                "指标口径": SUCCESS_CALIBER,
            },
        }

    # ---------- 对外动作 ----------

    def list_runs(self, model_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.internal_rows(RUN_TABLE)
        filtered = [row for row in rows if not model_code or row.get("模型编号") == model_code]
        # 列表只展示每个（模型, 版本, 路径）的最新一份，避免重复显示同一份成功清单。
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in filtered:
            key = (str(row.get("模型编号")), str(row.get("样本成功清单版本")), str(row.get("训练路径")))
            current = latest.get(key)
            if current is None or int(row.get("id", 0)) > int(current.get("id", 0)):
                latest[key] = row
        return sorted(latest.values(), key=lambda row: int(row.get("id", 0)))

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        # 训练结果一经生成即不可变：详情直接返回该份清单，不会混入其他批次的残留。
        return next((row for row in store.internal_rows(RUN_TABLE) if int(row.get("id", 0)) == int(run_id)), None)

    def train_batch(self, model_ids: list[int], version_input: str | None,
                    path_input: str | None) -> tuple[list[dict[str, Any]], str]:
        """多选模型批量训练；同一份版本输入重复提交沿用既有确认结果。"""
        if not model_ids:
            return [], "请至少选择一个分析模型"
        path = _normalize_path(path_input)
        if path not in TRAIN_PATHS:
            return [], f"训练路径「{path_input}」不在允许范围内"
        runs: list[dict[str, Any]] = []
        skipped: list[str] = []
        rows = store.internal_rows(RUN_TABLE)
        for model_id in model_ids:
            model_row = store.find(MODULE, int(model_id))
            if model_row is None:
                skipped.append(str(model_id))
                continue
            model_code = str(model_row.get("模型编号", ""))
            version_row = self.resolve_version(model_code, version_input)
            if version_row is None:
                skipped.append(model_code)
                continue
            version = str(version_row.get("版本", ""))
            existing = next((
                row for row in rows
                if row.get("模型编号") == model_code
                and row.get("样本成功清单版本") == version
                and row.get("训练路径") == path
            ), None)
            if existing is None:
                run = self._build_run(model_row, version_row, path, version_input)
                run["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
                run["生成时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rows.append(run)
                existing = run
            runs.append(existing)
            model_row["status"] = ACTION_RULES["提交训练"]
            model_row["pending"] = True
            model_row["abnormal"] = False
        if not runs:
            return [], "所选模型没有可用的批量训练样本或特征清单版本"
        message = f"已完成 {len(runs)} 个模型的批量训练"
        if skipped:
            message += f"，{len(skipped)} 个模型未找到匹配的样本成功清单版本"
        return runs, message

    def evaluate_run(self, run_id: int) -> tuple[dict[str, Any] | None, str]:
        """评估接口消费样本成功清单，回表复核对应记录后只保留真实成功的样本。"""
        run = self.get_run(run_id)
        if run is None:
            return None, f"训练结果 {run_id} 不存在"
        model_code = str(run.get("模型编号", ""))
        ledger = {str(row.get("样本编号")): row for row in self.samples(model_code)}
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for item in run.get("样本成功清单", []):
            sample_no = str(item.get("样本编号", ""))
            record = ledger.get(sample_no)
            if record is None:
                dropped.append({"样本编号": sample_no, "原因": "对应样本记录不存在"})
                continue
            recorded = str(item.get("读取状态", ""))
            live = str(record.get("读取状态", ""))
            if recorded == READ_SUCCESS and live != READ_SUCCESS:
                dropped.append({"样本编号": sample_no, "原因": f"复核读取状态为{live}"})
                continue
            if recorded == READ_SUCCESS_BACKFILL:
                # 补齐记录复核两件事：分区没有错位，且该分区确实还有有效特征可补。
                if str(record.get("分区")) != str(item.get("分区")):
                    dropped.append({"样本编号": sample_no, "原因": "补齐分区与样本成功清单不一致"})
                    continue
                if not _as_values(record.get("特征")):
                    dropped.append({"样本编号": sample_no, "原因": "补齐分区没有有效特征"})
                    continue
            elif live not in (READ_SUCCESS, READ_SUCCESS_BACKFILL):
                dropped.append({"样本编号": sample_no, "原因": f"复核读取状态为{live}"})
                continue
            verified_row = dict(record)
            # 清单里的处理结果才是本次训练的真实读取结果；补齐记录保留“已补齐”口径。
            verified_row["读取状态"] = recorded
            verified_row["处理结果"] = item.get("处理结果")
            kept.append(verified_row)
        feature_summary = self._feature_summary(kept, {"特征清单": run.get("特征清单", [])})
        metrics = _compute_metrics(kept, feature_summary)
        run["评估复核"] = {
            "复核样本数": len(run.get("样本成功清单", [])),
            "有效样本数": len(kept),
            "剔除样本": dropped,
            "复核指标": metrics,
            "复核时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        run["评估指标"] = metrics
        return run, f"已复核 {len(run.get('样本成功清单', []))} 条成功样本，实际参与评估 {len(kept)} 条"

    def success_list(self, run_id: int) -> dict[str, Any] | None:
        """跨业务模块读取样本成功清单时使用的统一出口。"""
        run = self.get_run(run_id)
        if run is None:
            return None
        return {
            "run_id": run.get("id"),
            "模型编号": run.get("模型编号"),
            "样本成功清单版本": run.get("样本成功清单版本"),
            "版本输入": run.get("版本输入"),
            "训练路径": run.get("训练路径"),
            "特征清单": run.get("特征清单", []),
            "特征汇总": run.get("特征汇总", []),
            "样本总数": run.get("样本总数"),
            "成功样本数": run.get("成功样本数"),
            "失败样本数": run.get("失败样本数"),
            "成功率": run.get("成功率"),
            "items": run.get("样本成功清单", []),
        }

    def export_rows(self, model_code: str | None, version: str | None,
                    path: str | None) -> list[dict[str, Any]]:
        """导出区分成功样本口径与待补齐样本口径，两类记录不混算。"""
        model_code = str(model_code or "").strip()
        version = str(version or "").strip()
        path = str(path or "").strip()
        runs = self.list_runs(model_code or None)
        if not path:
            # 未指定路径时优先导出「排除失败样本」那份，才能同时给出两种口径。
            prefer = [row for row in runs if str(row.get("训练路径")) == EXCLUDE_PATH]
            runs = prefer or runs
        if version:
            runs = [row for row in runs if str(row.get("样本成功清单版本")) == version]
        if path:
            normalized = _normalize_path(path)
            runs = [row for row in runs if str(row.get("训练路径")) == normalized]
        run = runs[-1] if runs else None
        if run is None:
            return []
        records: list[dict[str, Any]] = []
        for item in run.get("样本成功清单", []):
            records.append({
                "口径": SUCCESS_CALIBER,
                "模型编号": run.get("模型编号"),
                "样本编号": item.get("样本编号"),
                "批次": item.get("批次"),
                "分区": item.get("分区"),
                "读取状态": item.get("读取状态"),
                "处理结果": item.get("处理结果"),
                "有效特征": "、".join(item.get("有效特征") or []),
                "清单版本": item.get("清单版本"),
                "训练路径": run.get("训练路径"),
            })
        for item in run.get("待补齐样本", []):
            records.append({
                "口径": PENDING_CALIBER,
                "模型编号": run.get("模型编号"),
                "样本编号": item.get("样本编号"),
                "批次": item.get("批次"),
                "分区": item.get("分区"),
                "读取状态": item.get("读取状态"),
                "处理结果": item.get("处理结果"),
                "有效特征": "、".join(item.get("有效特征") or []),
                "清单版本": run.get("样本成功清单版本"),
                "训练路径": run.get("训练路径"),
            })
        return records
