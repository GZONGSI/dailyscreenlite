"""尚未迁移功能的响应装配，隔离数据库行结构与前端契约。

已迁移功能的 DTO 由各路由模块拥有（`app/routes/notes.py`、`app/routes/classification.py`、
`app/routes/observations.py`），前端类型由它们生成；这里只保留还在用宽泛结构的端点：
导入批次与行情。
"""

from __future__ import annotations


def stock_outcome_json(stock, *, name: str | None = None) -> dict:
    return {
        "position": stock.position,
        "rawCode": stock.raw_code,
        "normalizedCode": stock.normalized_code,
        "outcome": stock.outcome.value,
        "securityId": stock.security_id,
        # 名称来自权威证券库；未识别的行没有名称
        "name": name,
        "reason": stock.reason,
        # 该行对候选项队列的影响：new / merged / reopened / source_only / None
        "effect": getattr(stock, "effect", None),
        # 来源附带字段的最小只读存档，不参与证券识别或行情计算
        "rawExtras": dict(stock.raw_extras),
    }


def selection_json(selection) -> dict | None:
    if selection is None:
        return None
    return selection.to_dict()


def batch_json(batch, *, include_stocks: bool = True, names: dict[str, str] | None = None) -> dict:
    payload = {
        "batchId": batch.batch_id,
        "sourceKind": batch.source_kind.value,
        "sourceName": batch.source_name,
        "sourceRef": batch.source_ref,
        "receivedAt": batch.received_at.isoformat(),
        "importDate": batch.import_date.iso,
        "status": batch.status.value,
        "declaredTotal": batch.declared_total,
        "parsedCount": batch.parsed_count,
        "uniqueCount": batch.unique_count,
        "recognizedCount": batch.recognized_count,
        "skippedCount": batch.skipped_count,
        # 本批次对候选队列的影响：新增候选项、合并到未处理候选项、重新进入待归类
        "newCandidateCount": batch.new_candidate_count,
        "mergedCandidateCount": batch.merged_candidate_count,
        "reopenedCandidateCount": batch.reopened_candidate_count,
        "errorCode": batch.error_code,
        "errorMessage": batch.error_message,
        "queryText": batch.query_text,
        "conditionLabels": list(batch.condition_labels),
        "identityFingerprint": batch.identity_fingerprint,
        "completeness": batch.completeness,
        "selection": selection_json(batch.selection),
        "reidentifiedAt": batch.reidentified_at.isoformat() if batch.reidentified_at else None,
        "reidentifiedImported": batch.reidentified_imported,
        "reidentifiedRemoved": batch.reidentified_removed,
    }
    if include_stocks:
        payload["stocks"] = [
            stock_outcome_json(s, name=(names or {}).get(s.security_id or ""))
            for s in batch.stocks
        ]
    return payload
