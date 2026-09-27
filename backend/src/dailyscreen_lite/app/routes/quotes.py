"""行情与数据更新路由：读取日线、手动更新与更新状态。只调用应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from dailyscreen_lite.domain.models import UpdateKind
from dailyscreen_lite.quotes.update import UpdateBusy, run_json

router = APIRouter(prefix="/api", tags=["quotes"])


def _container(request: Request):
    return request.app.state.container


@router.get("/quotes/summary")
def quote_summaries(securityIds: str, request: Request) -> dict:
    """批量轻量行情摘要：队列列表的最新收盘价、行情日与日涨跌幅。

    只读取最近两个交易日，不为列表加载整段历史；缺失的股票不返回条目。
    """
    ids = [part.strip() for part in securityIds.split(",") if part.strip()]
    snapshots = _container(request).quotes.snapshots(ids)
    return {
        "quotes": [
            {
                "securityId": snapshot.security_id,
                "tradeDate": snapshot.trade_date,
                "close": snapshot.close,
                "changePct": snapshot.change_pct,
            }
            for snapshot in snapshots.values()
        ]
    }


@router.get("/quotes/{security_id}")
def quote_view(
    security_id: str,
    request: Request,
    limit: int = Query(default=250, ge=1, le=2000),
    end: str | None = None,
) -> dict:
    """股票日线：默认最近约 250 个交易日，可用 end 向前浏览更早历史。"""
    view = _container(request).quotes.view(security_id, limit=limit, end=end)
    return {
        "securityId": view.security_id,
        "available": view.available,
        "reason": view.reason,
        "adjust": view.adjust,
        "adjustLabel": view.adjust_label,
        "source": view.source,
        "latestDate": view.latest_date,
        "earliestDate": view.earliest_date,
        "total": view.total,
        "fetchedAt": view.fetched_at,
        "bars": [
            {
                "date": bar.trade_date.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volumeLots": bar.volume_lots,
                "amountYuan": bar.amount_yuan,
            }
            for bar in view.bars
        ],
    }


@router.get("/updates")
def update_status(request: Request) -> dict:
    """当前更新状态与最近一次结果（含数据日期），重启后可读。"""
    service = _container(request).updates
    payload = service.status()
    payload["history"] = service.history(5)
    return payload


@router.post("/updates")
def trigger_update(request: Request) -> dict:
    """手动更新：触发一次统一更新流程；已在运行时不并发执行。"""
    service = _container(request).updates
    if service.running:
        raise HTTPException(status_code=409, detail={"message": "更新正在进行中"})
    try:
        record = service.run(UpdateKind.MANUAL)
    except UpdateBusy as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    return run_json(record)


# --- 数据中心 ---

@router.get("/data/status")
def data_status(request: Request) -> dict:
    """轻量数据状态：顶部未补齐提醒、证券库提示与分项结论。

    业务页面每次进入都读它，所以不带逐股明细；明细在 /api/data。
    证券库结论取自最近一次真正动过证券库的更新：其后的定向补取与单股更新记 skipped，
    不应把上一次的失败提示说成「本次未更新证券库」。
    导入补取执行中显示进度文案，结束后恢复按目标交易日完整性判定的提醒。
    """
    container = _container(request)
    status = container.data_status.status()
    securities = status.securities_run
    payload = _summary_json(status)
    payload["securitiesMessage"] = securities.securities_message if securities else None
    payload["securitiesFailed"] = bool(securities and securities.securities_failed)
    payload["updating"] = container.updates.running
    if container.updates.import_updating:
        payload["reminder"] = "导入股票数据更新中"
    return payload


@router.get("/data")
def data_center(request: Request) -> dict:
    """数据中心：未补齐提醒、分项状态、逐股结果、失败原因、更新记录与重试入口。"""
    container = _container(request)
    status = container.data_status.status()
    last = status.last_run
    securities = status.securities_run
    payload = _summary_json(status)
    payload.update(
        {
            "scopeCount": status.scope_count,
            "scopeLabel": "待归类股票与观察组股票",
            "securitiesStatus": securities.securities_status if securities else None,
            "securitiesMessage": securities.securities_message if securities else None,
            "securitiesCount": securities.securities_count if securities else 0,
            "stocks": [stock_json(stock) for stock in status.stocks],
            "retryIds": [stock.security_id for stock in status.incomplete],
            "progress": container.updates.progress(),
            "quoteDiagnostics": [
                {
                    "securityId": item.security_id,
                    "fetchMode": item.fetch_mode,
                    "requestStart": item.request_start,
                    "elapsedMs": item.elapsed_ms,
                    "attempts": list(item.attempts),
                }
                for item in status.quote_diagnostics
            ],
            "lastRun": run_json(last),
            "history": [run_json(run) for run in status.history],
            "automaticRounds": container.updates.automatic_rounds(),
        }
    )
    return payload


@router.post("/data/retry")
def retry_incomplete(request: Request) -> dict:
    """手动重试未完成部分：只重取待补齐与状态待确认的股票，不整体重跑。"""
    container = _container(request)
    ids = container.data_status.incomplete_ids()
    if not ids:
        return {"started": False, "count": 0, "quotesOk": 0}
    if container.updates.running:
        raise HTTPException(status_code=409, detail={"message": "更新正在进行中"})
    ok = container.updates.refresh_quotes(ids, UpdateKind.RETRY)
    return {"started": True, "count": len(ids), "quotesOk": ok}


@router.post("/securities/{security_id}/refresh")
def refresh_single_security(security_id: str, request: Request) -> dict:
    """历史详情的单股手动更新：只更新这一只，不加入持续更新范围。"""
    container = _container(request)
    if container.updates.running:
        raise HTTPException(status_code=409, detail={"message": "更新正在进行中"})
    ok = container.updates.refresh_security(security_id)
    view = container.quotes.view(security_id, limit=1)
    return {
        "securityId": security_id,
        "updated": ok > 0,
        "available": view.available,
        "latestDate": view.latest_date,
    }


def _summary_json(status) -> dict:
    """提醒与分项结论：轻量接口与数据中心共用，避免两处字段漂移。"""
    return {
        "reminder": status.reminder,
        "complete": status.complete,
        "targetTradeDate": (
            status.target_trade_date.isoformat() if status.target_trade_date else None
        ),
        "targetSource": status.target_source,
        "calendarAvailable": status.calendar_available,
        "incompleteCount": len(status.incomplete),
        # 分项顺序由判定层给出（证券库、股票状态、日线），此处不重排
        "items": [item_json(item) for item in status.items],
    }


def item_json(item) -> dict:
    return {
        "key": item.key,
        "label": item.label,
        "state": item.state,
        "message": item.message,
    }


def stock_json(stock) -> dict:
    return {
        "securityId": stock.security_id,
        "name": stock.name,
        "state": stock.state.value,
        "marketStatus": stock.market_status.value,
        "latestDate": stock.latest_date,
        "reason": stock.reason,
        "lastError": stock.last_error,
        "source": stock.source,
    }
