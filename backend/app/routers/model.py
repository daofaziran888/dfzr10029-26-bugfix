"""分析模型 DAG 拓扑接口：返回节点、连线图结构，并提供批量训练样本链路。"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.model import ModelService

router = APIRouter(prefix="/api/model", tags=["分析模型"])
service = ModelService()
LIST_FIELDS = ["模型编号", "模型名称", "模型类型", "训练数据集", "特征字段", "目标字段", "评估指标", "模型状态"]
STATUSES = ["草稿", "已训练", "评估中", "已部署", "已下线"]


class BatchTrainPayload(BaseModel):
    """批量训练入参：多选模型 + 样本成功清单版本 + 训练路径。

    版本字段兼容 `version` / `sample_version` / `success_list_version` / `version_input`，
    路径字段兼容 `path` / `train_path` / `strategy`，
    模型标识兼容数字 id 与模型编号，便于既有调用方直接接入。
    """

    model_ids: list[Any] = Field(default_factory=list)
    ids: list[Any] = Field(default_factory=list)
    models: list[Any] = Field(default_factory=list)
    model_codes: list[str] = Field(default_factory=list)
    version: str | None = None
    sample_version: str | None = None
    success_list_version: str | None = None
    version_input: str | None = None
    path: str | None = None
    train_path: str | None = None
    strategy: str | None = None

    def resolved_ids(self) -> list[int]:
        nodes = service.get_graph()["nodes"]
        by_code = {str(row.get("模型编号")): int(row["id"]) for row in nodes}
        by_id = {str(row["id"]): int(row["id"]) for row in nodes}
        ids: list[int] = []
        for raw in list(self.model_ids or self.ids or self.models) + list(self.model_codes):
            text = str(raw).strip()
            resolved = by_code.get(text) or by_id.get(text)
            if resolved is None:
                try:
                    resolved = int(text)
                except (TypeError, ValueError):
                    continue
            if resolved not in ids:
                ids.append(resolved)
        return ids

    def resolved_version(self) -> str | None:
        for value in (self.version, self.sample_version, self.success_list_version, self.version_input):
            if value and str(value).strip():
                return str(value).strip()
        return None

    def resolved_path(self) -> str | None:
        for value in (self.path, self.train_path, self.strategy):
            if value and str(value).strip():
                return str(value).strip()
        return None


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def get_graph() -> dict[str, Any]:
    """返回节点与边组成的 DAG 图数据。"""
    return service.get_graph()


@router.get("/versions")
def list_versions(
    version: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    versions: list[dict[str, Any]] = []
    for model in service.get_graph()["nodes"]:
        model_code = str(model.get("模型编号", ""))
        for row in service.feature_versions(model_code):
            if version and str(row.get("版本")) != version:
                continue
            versions.append({
                "模型编号": model_code,
                "模型名称": model.get("模型名称"),
                "版本": row.get("版本"),
                "版本状态": row.get("版本状态"),
                "特征清单": row.get("特征清单"),
                "特征数量": row.get("特征数量"),
                "生效时间": row.get("生效时间"),
            })
    total = len(versions)
    start = (page - 1) * size
    return {"items": versions[start:start + size], "total": total, "page": page, "size": size}


@router.get("/training")
def list_training(
    model_code: str | None = Query(default=None),
    version: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """训练结果列表：按（模型, 成功清单版本, 路径）去重，避免同份清单重复显示。"""
    items = service.list_runs(model_code)
    if version:
        items = [row for row in items if str(row.get("样本成功清单版本")) == version]
    total = len(items)
    start = (page - 1) * size
    return {"items": items[start:start + size], "total": total, "page": page, "size": size}


@router.post("/training/batch")
def train_batch(payload: BatchTrainPayload) -> dict[str, Any]:
    """多选记录批量训练：样本成功清单与特征汇总一起生成，只基于成功样本。"""
    runs, message = service.train_batch(
        payload.resolved_ids(), payload.resolved_version(), payload.resolved_path())
    if not runs:
        return {"ok": False, "message": message, "items": []}
    return {"ok": True, "message": message, "items": runs}


@router.get("/training/export")
def export_training(
    model_code: str | None = Query(default=None),
    version: str | None = Query(default=None),
    path: str | None = Query(default=None),
) -> StreamingResponse:
    """导出训练清单：口径列区分「成功样本」与「待补齐样本」。"""
    records = service.export_rows(model_code, version, path)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    columns = ["口径", "模型编号", "样本编号", "批次", "分区", "读取状态", "处理结果", "有效特征", "清单版本", "训练路径"]
    writer.writerow(columns)
    for record in records:
        row = []
        for column in columns:
            text = str(record.get(column, "") if record.get(column) is not None else "")
            # 保留 csv.writer 的转义，仅对公式型前缀做防注入处理。
            row.append("'" + text if text and text[0] in "=+-@" else text)
        writer.writerow(row)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=model-training.csv"})


@router.get("/training/{run_id}/success-list")
def get_success_list(run_id: int) -> dict[str, Any]:
    """跨业务模块读取样本成功清单的统一出口：清单与特征汇总同时返回。"""
    payload = service.success_list(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"训练结果 {run_id} 不存在")
    return payload


@router.get("/training/{run_id}")
def get_training(run_id: int) -> dict[str, Any]:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"训练结果 {run_id} 不存在")
    return run


@router.post("/training/{run_id}/evaluate")
def evaluate_training(run_id: int) -> dict[str, Any]:
    """模型评估接口：消费样本成功清单，复核对应记录后只按实际成功样本计算指标。"""
    run, message = service.evaluate_run(run_id)
    if run is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "entry": run}


@router.get("/export")
def export_entries() -> StreamingResponse:
    nodes, edges = service.raw_data()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["type", "id", "name", "from", "to", "label", "status"])
    for node in nodes:
        writer.writerow(["node", node.get("id",""), csv_cell(node.get("name","")), "", "", "", node.get("status","")])
    for edge in edges:
        writer.writerow(["edge", "", "", edge.get("from",""), edge.get("to",""), edge.get("label",""), ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=model.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"模型配置 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="模型配置已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
