"""问财真实 HTTP 会话（标准库 urllib）。

对接问财网页当前使用的流式接口 `gateway/aime/stream-query`（SSE）：
- 一次查询即取回全部结果行（来源按 perpage 截断，故按声明总数足量请求），
  解析条件取响应内 `model_sql`（其中的窗口为 `recent('120d')`/`front('250t')`
  这类相对表述，不含滚动日期，口径指纹跨日稳定）；
- 鉴权失败（HTTP 401/403）与业务错误（status_code != 0）分别归类，不解读为零结果；
- 只取股票代码与来源声明的总数、条件，不做选股语义改写。

cookie 与令牌只出现在请求头，从不进入日志、报告或批次记录。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from dailyscreen_lite.domain.errors import (
    WencaiAuthError,
    WencaiLoginRequired,
    WencaiUnexpected,
)
from dailyscreen_lite.wencai.acquisition import QueryContext
from dailyscreen_lite.wencai.token import NodeTokenProvider

DEFAULT_BASE_URL = "https://www.iwencai.com"
# 网页当前使用的流式结果接口；基址可配置（镜像或本地桩），路径保持不变
_STREAM_PATH = "/gateway/aime/stream-query"
# 一次请求的默认行数上限：A 股全市场约 5500 只，6000 足以覆盖常规候选集合
_STREAM_PERPAGE = 6000
# 若来源声明的总数超过默认上限，按总数再取一次；上限防止异常请求放大
_MAX_STREAM_PERPAGE = 20000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
LOGIN_MARKERS = ("请登录", "重新登录", "登录已过期", "登录后查看", "未登录", "未登陆")


class HttpWencaiSession:
    def __init__(
        self,
        cookie: str,
        tokens: NodeTokenProvider,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._cookie = cookie
        self._tokens = tokens
        self._timeout = timeout
        root = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._stream_url = root + _STREAM_PATH

    # --- 协议实现 ---

    def parse(self, query: str) -> QueryContext:
        data, token = self._fetch(query, _STREAM_PERPAGE)
        # 来源按 perpage 截断：若没取全且总数在放大上限内，按总数再取一次。
        # 重取后整份响应（行、总数、条件）都采用第二次，绝不把两次响应拼在一起：
        # 否则会用第二次的总数/条件去判定第一次的行，把不一致误判为完整。
        total = _declared_total(data)
        if isinstance(total, int) and total > len(_rows(data)) and total <= _MAX_STREAM_PERPAGE:
            data, token = self._fetch(query, total)
        return QueryContext(
            query=query,
            condition=_condition(data),
            declared_total=_declared_total(data),
            # 行已在解析阶段取回，page() 只做切片；保留来源 token 便于排查
            page_context={"source": "stream-query", "token": token} if token else {"source": "stream-query"},
            comp_id=None,
            uuid=None,
            rows=tuple(_rows(data)),
        )

    def page(self, context: QueryContext, *, page: int, perpage: int) -> list[dict]:
        rows = context.rows or ()
        start = (page - 1) * perpage
        if start >= len(rows):
            return []
        return [dict(row) for row in rows[start : start + perpage]]

    # --- 内部 ---

    def _fetch(self, query: str, perpage: int) -> tuple[dict, str | None]:
        """请求一次流式结果，返回 (结果组件 data, token)。

        响应必须"明确成功"：业务状态码显式为 0。状态缺失或非 0 属获取/解析失败，
        即使声明零总数也不能当作真实零结果（A10）。结果行的合法性由 _rows 校验。
        """
        payload = self._post_stream(query, perpage)
        data = _result_data(payload)
        if data is None:
            raise WencaiUnexpected("问财响应缺少结果组件，不解读为零结果")
        status = data.get("status_code")
        if status is None:
            raise WencaiUnexpected("问财响应未提供成功状态，不解读为零结果")
        if status not in (0, "0"):
            raise WencaiUnexpected(
                f"问财返回业务错误：{data.get('status_msg') or status}",
            )
        token = data.get("token")
        return data, str(token) if token else None

    def _post_stream(self, query: str, perpage: int) -> list[dict]:
        body = {
            "question": query,
            "default_fallback": False,
            "input_type": "click",
            "entity_info": {"device_type": "pc", "comefrom": None},
            "source": "ths_iwencai_pc_xuangu",
            "dialog_model": "CUSTOMER_AGENT",
            "version": "3.4.1",
            "agent_tools": [{"tool_id": "FinQuery", "tool_param": {"domain": "stock", "perpage": perpage}}],
            "events": [{"event_type": "user_input", "event_name": "normal_agent", "content": {}}],
            "add_info": {},
            "agent_id": "MaSzyUwyyl",
            "agent_name": "",
        }
        request = urllib.request.Request(
            self._stream_url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://www.iwencai.com",
                "Referer": "https://www.iwencai.com/screener/result?w="
                + urllib.parse.quote(query)
                + "&querytype=stock",
                "hexin-v": self._tokens.token(),
                "cookie": self._cookie,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise WencaiUnexpected(f"问财请求失败：{type(exc.reason).__name__}") from exc
        events = list(_iter_sse(text))
        _reject_login_markers(events)
        return events


def _classify_http_error(exc: urllib.error.HTTPError) -> Exception:
    if exc.code in (401, 403):
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            message = body.get("status_msg") or body.get("message")
            if isinstance(message, str) and message.strip():
                detail = f"：{message.strip()}"
        except Exception:  # noqa: BLE001 - 响应体不可解析时只报状态码
            detail = ""
        return WencaiAuthError(f"问财鉴权或访问异常（HTTP {exc.code}）{detail}，不解读为零结果")
    return WencaiUnexpected(f"问财请求返回 HTTP {exc.code}")


def _reject_login_markers(events: list[dict]) -> None:
    """只检查状态/错误字段，绝不扫描结果行。

    结果行里可能出现"未登录"等字样的股票简称；扫描行数据会把正常结果误判为
    需要登录（A10：鉴权异常要按实际证据，不得一律称为会话过期）。
    """
    status_fields: list[str] = []
    for event in events:
        payload = _result_data([event])
        if payload is not None:
            for key in ("status_msg", "status_code", "error", "message"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    status_fields.append(value)
        for key in ("type", "answer_path"):
            value = event.get(key)
            if isinstance(value, str):
                status_fields.append(value)
    blob = " ".join(status_fields)
    for marker in LOGIN_MARKERS:
        if marker in blob:
            raise WencaiLoginRequired(f"问财响应提示需要登录：{marker}")


def _iter_sse(text: str) -> list[dict]:
    """解析 SSE 文本，返回其中的 JSON 事件。非 JSON 行忽略，不当作结果。"""
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _result_data(events: list[dict]) -> dict | None:
    """从事件里取结果组件 data；要求同时含行数据与总数，避免误取其他组件。"""
    for event in events:
        components = _dig(event, ("section", "result_page", "components"))
        if not isinstance(components, list):
            continue
        for component in components:
            data = component.get("data") if isinstance(component, dict) else None
            if not isinstance(data, dict):
                continue
            if "datas" in data or "code_count" in data or "row_count" in data:
                return data
    return None


def _rows(data: dict) -> list[dict]:
    """取出结果行并校验结构；非法即视为解析失败，绝不静默丢弃。

    静默过滤会把「非空但结构异常的列表」变成空列表，从而绕过
    "零结果必须明确成功且空列表"的校验（A10）。缺 datas 或行不是对象都拒绝。
    """
    datas = data.get("datas")
    if not isinstance(datas, list):
        raise WencaiUnexpected("问财响应缺少结果行数据，不解读为零结果")
    rows: list[dict] = []
    for row in datas:
        if not isinstance(row, dict):
            raise WencaiUnexpected("问财结果行结构异常，不解读为零结果")
        rows.append(row)
    return rows


def _declared_total(data: dict) -> int | None:
    for key in ("code_count", "row_count"):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _condition(data: dict) -> object | None:
    """解析条件取响应内的 model_sql（JSON 字符串）；取不到时返回 None。

    无法取得条件结构时按"口径未知"处理，迫使界面要求用户确认，而不是自动发布。
    """
    raw = data.get("model_sql")
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _dig(node: object, path: tuple) -> object:
    current = node
    for part in path:
        if isinstance(current, list) and isinstance(part, int):
            current = current[part] if part < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current
