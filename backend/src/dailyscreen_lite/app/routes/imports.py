"""导入相关路由。只调用应用服务，不直接访问数据库。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from dailyscreen_lite.app.schemas import batch_json
from dailyscreen_lite.domain.errors import ImportRejected, UnsupportedFormat

router = APIRouter(prefix="/api/imports", tags=["imports"])

_CSV_SUFFIXES = {".csv", ".tsv"}


def _imports(request: Request):
    return request.app.state.container.imports


def _bad_request(error: Exception) -> HTTPException:
    code = getattr(error, "code", None) or "invalid_request"
    message = getattr(error, "message", None) or str(error)
    return HTTPException(status_code=400, detail={"code": code, "message": message})


@router.get("")
def list_batches(request: Request, importDate: str | None = None) -> dict:
    batches = _imports(request).list_batches(importDate)
    return {"batches": [batch_json(b, include_stocks=False) for b in batches]}


@router.post("")
async def submit_sources(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    texts: list[str] = Form(default=[]),
) -> dict:
    """一次提交混合来源：多个文件与多个文本块各自成批次，互不影响。

    同一次提交共用同一个接收时刻（导入日期固定），单个来源失败不阻止其他来源。
    """
    service = _imports(request)
    payloads: list[tuple[str, bytes]] = []
    for upload in files:
        payloads.append((upload.filename or "source.csv", await upload.read()))
    results = [
        batch_json(batch)
        for batch in service.submit_sources(payloads, list(texts))
    ]
    if not results:
        raise _bad_request(ImportRejected("没有可提交的来源"))
    return {"batches": results}


@router.get("/{batch_id}")
def get_batch(batch_id: str, request: Request) -> dict:
    service = _imports(request)
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"message": "批次不存在"})
    return batch_json(batch, names=service.batch_security_names(batch))


@router.post("/csv")
async def upload_csv(request: Request, file: UploadFile = File(...)) -> dict:
    """单文件入口（保留以兼容既有调用）；格式不支持时该批次记为未发布。"""
    name = file.filename or "source.csv"
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in _CSV_SUFFIXES:
        raise _bad_request(UnsupportedFormat(f"暂不支持的文件格式：{suffix or name}"))
    data = await file.read()
    return batch_json(_imports(request).submit_file(name, data))


@router.post("/{batch_id}/selection")
def resolve_selection(batch_id: str, request: Request, payload: dict) -> dict:
    """歧义选择：用户确认工作表或代码列后继续解析。"""
    sheet = payload.get("sheet")
    raw_col = payload.get("codeColumn")
    code_column = int(raw_col) if raw_col is not None else None
    return _run(
        lambda: _imports(request).resolve_selection(
            batch_id, sheet=sheet, code_column=code_column
        )
    )


@router.post("/{batch_id}/confirm")
def confirm_link(batch_id: str, request: Request) -> dict:
    """问财条件确认：记录查询身份并发布待确认批次。"""
    return _run(lambda: _imports(request).confirm_link(batch_id))


@router.post("/{batch_id}/reidentify")
def reidentify(batch_id: str, request: Request) -> dict:
    """重新识别原批次跳过项；再次未识别的明细直接删除。"""
    return _run(lambda: _imports(request).reidentify(batch_id))


@router.post("/{batch_id}/retry")
def retry_batch(batch_id: str, request: Request) -> dict:
    """重试未发布的失败批次，保留原批次身份与导入日期。"""
    return _run(lambda: _imports(request).retry(batch_id))


def _run(action) -> dict:
    """批次操作统一错误映射：不存在 → 404，状态不允许 → 400。"""
    try:
        return batch_json(action())
    except KeyError:
        raise HTTPException(status_code=404, detail={"message": "批次不存在"})
    except ValueError as exc:
        raise _bad_request(exc)
