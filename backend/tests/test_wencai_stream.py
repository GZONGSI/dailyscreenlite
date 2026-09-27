"""问财流式接口（stream-query / SSE）适配器测试。

用本地 SSE 桩替代外部站点，验证真实 HttpWencaiSession 的解析、分页、条件提取、
完整性与鉴权/业务错误分支；不访问外网。条件与集合语义由固定响应提供，
实源结论单列在工单 Comments。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dailyscreen_lite.domain.errors import WencaiAuthError, WencaiIncomplete, WencaiUnexpected
from dailyscreen_lite.wencai import http_session as session_mod
from dailyscreen_lite.wencai.acquisition import acquire
from dailyscreen_lite.wencai.http_session import HttpWencaiSession

URL = "https://www.iwencai.com/screener/result?w=%E5%88%9B%E6%96%B0%E9%AB%98&querytype=stock"

_CONDITION = json.dumps(
    {
        "logic_relation": [
            {
                "logic": "INTERSECT",
                "requirements": [
                    {"text": "最高价创近120天新高", "sql": "where record_high(最高价, recent('120d'))"},
                    {"text": "过去250个交易日区间涨跌幅<=100%", "sql": "where 交易日期 = front('250t')"},
                ],
            }
        ]
    },
    ensure_ascii=False,
)


def _row(code: str, name: str = "示例") -> dict:
    market = "SH" if code.startswith(("6", "9")) else ("BJ" if code.startswith("92") else "SZ")
    return {"code": code, "股票代码": f"{code}.{market}", "股票简称": name}


def _sse(data: dict, *, status_code: int = 200) -> bytes:
    events = [
        {"type": "base_info", "base_info": {"question": "创新高"}},
        {
            "answer_path": "other/openAnswer",
            "section": {"result_page": {"components": [{"data": data}]}},
        },
    ]
    body = "".join(f"data:{json.dumps(ev, ensure_ascii=False)}\n\n" for ev in events)
    return body.encode("utf-8")


class _Stub:
    """可配置的 SSE 桩：按总数与每次请求的 perpage 返回行，可注入 401 与业务错误。"""

    def __init__(
        self,
        *,
        total: int,
        condition: str | None = _CONDITION,
        http_status: int = 200,
        business_status: int = 0,
        name: str = "示例",
        omit_datas: bool = False,
        omit_status: bool = False,
        scripted: list[dict] | None = None,
    ):
        self.total = total
        self.condition = condition
        self.http_status = http_status
        self.business_status = business_status
        self.name = name
        self.omit_datas = omit_datas
        self.omit_status = omit_status
        # 按请求顺序返回固定响应体（用于构造"两次响应不一致"等组合）
        self.scripted = scripted
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: ANN002 - 静默
                return

            def do_POST(self):  # noqa: N802 - http.server 接口
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append(payload)
                if stub.http_status != 200:
                    body = json.dumps(
                        {"status": stub.http_status, "status_code": -1935, "status_msg": "未登陆,请登录后再试"}
                    ).encode("utf-8")
                    self.send_response(stub.http_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                perpage = 100
                try:
                    perpage = int(payload["agent_tools"][0]["tool_param"]["perpage"])
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
                if stub.scripted is not None:
                    index = min(len(stub.requests) - 1, len(stub.scripted) - 1)
                    data = dict(stub.scripted[index])
                else:
                    rows = [
                        _row(f"{600000 + i:06d}", stub.name)
                        for i in range(min(perpage, stub.total))
                    ]
                    data = {
                        "chunks_info": json.dumps([f"条件下命中 ({stub.total})"], ensure_ascii=False),
                        "code_count": stub.total,
                        "row_count": stub.total,
                        "dataSize": len(rows),
                        "status_code": stub.business_status,
                        "status_msg": "请求正常" if stub.business_status == 0 else "查询被拒绝",
                        "token": "stub-token",
                        "datas": rows,
                    }
                    if stub.condition is not None:
                        data["model_sql"] = stub.condition
                if stub.omit_datas:
                    data.pop("datas", None)
                if stub.omit_status:
                    data.pop("status_code", None)
                body = _sse(data)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class _Tokens:
    def token(self) -> str:
        return "stub-hexin-v"


@pytest.fixture
def stub():
    stubs: list[_Stub] = []

    def make(**kwargs) -> _Stub:
        s = _Stub(**kwargs)
        s.start()
        stubs.append(s)
        return s

    yield make
    for s in stubs:
        s.stop()


def _session(stub: _Stub) -> HttpWencaiSession:
    return HttpWencaiSession("cookie=1", _Tokens(), base_url=stub.base_url)


def test_stream_session_parses_rows_condition_and_total(stub):
    s = stub(total=23)
    session = _session(s)
    result = acquire(URL, session, perpage=100)

    assert result.declared_total == 23
    assert len(result.candidates) == 23
    assert result.completeness == "internally_consistent"
    # 条件取来源自身解析（model_sql 的 text），去掉"选股票,"前缀
    assert result.condition_labels == ("最高价创近120天新高", "过去250个交易日区间涨跌幅<=100%")
    assert result.condition_fingerprint is not None


def test_stream_session_pages_without_losing_rows(stub):
    """总量超过一页时按 page 切片，完整性判定仍成立。"""
    s = stub(total=250)
    session = _session(s)
    result = acquire(URL, session, perpage=100)
    assert len(result.candidates) == 250
    assert result.completeness == "internally_consistent"
    # 只解析一次（一次取回全部行），后续页为本地切片，不重复请求来源
    assert len(s.requests) == 1


def test_stream_session_refetches_when_first_request_truncated(stub, monkeypatch):
    """默认请求被来源按上限截断时，按声明总数再取一次。"""
    monkeypatch.setattr(session_mod, "_STREAM_PERPAGE", 2)
    s = stub(total=5)
    session = _session(s)
    result = acquire(URL, session, perpage=100)
    assert len(result.candidates) == 5
    # 第一次 perpage=2 被截断，第二次按总数 5 再取
    assert len(s.requests) == 2
    assert s.requests[1]["agent_tools"][0]["tool_param"]["perpage"] == 5


def test_stream_session_zero_result_is_confirmed_not_empty(stub):
    s = stub(total=0)
    result = acquire(URL, _session(s), perpage=100)
    assert result.completeness == "confirmed_zero_result"
    assert result.candidates == ()


def test_stream_session_without_condition_blocks_zero_result(stub):
    """零结果但取不到条件结构时不能确认为真实零结果。"""
    s = stub(total=0, condition=None)
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_stream_session_auth_error_is_not_zero_result(stub):
    s = stub(total=0, http_status=401)
    with pytest.raises(WencaiAuthError):
        acquire(URL, _session(s), perpage=100)


def test_stream_session_declared_shortfall_is_incomplete(stub, monkeypatch):
    """来源只给部分行且放大后被截断：唯一数少于声明总数，判为不完整。"""
    monkeypatch.setattr(session_mod, "_STREAM_PERPAGE", 2)
    monkeypatch.setattr(session_mod, "_MAX_STREAM_PERPAGE", 2)  # 阻止按总数放大
    s = stub(total=120)
    with pytest.raises(WencaiIncomplete):
        acquire(URL, _session(s), perpage=100)


def test_large_result_is_not_truncated_by_page_cap(stub, monkeypatch):
    """总行数超过 MAX_PAGES × perpage 时不得被判为不完整。

    来源已一次取回全部行；分页上限只能约束"向来源请求次数"，
    不能截断已有数据，否则完整结果会被误报为分页缺失。
    回归：acquire 曾按 perpage=100、MAX_PAGES=50 切片，只取到 5000 行。
    """
    monkeypatch.setattr("dailyscreen_lite.wencai.acquisition.MAX_PAGES", 2)
    s = stub(total=6000)
    result = acquire(URL, _session(s), perpage=100)
    assert result.declared_total == 6000
    assert len(result.candidates) == 6000
    assert result.completeness == "internally_consistent"


def test_login_marker_in_row_value_is_not_auth_error(stub):
    """结果行里的"未登录"字样不得被误判为需要登录。

    鉴权异常按实际证据（HTTP 状态/状态字段）判定，不能扫描行数据。
    """
    s = stub(total=2, name="未登录测试")  # 股票简称本身含登录字样
    result = acquire(URL, _session(s), perpage=100)
    assert len(result.candidates) == 2
    assert result.completeness == "internally_consistent"


def test_missing_datas_is_not_confirmed_zero_result(stub):
    """结果行字段缺失属解析失败：即使声明零总数，也不能当作真实零结果。

    A10：获取或解析失败不能变成零结果；真实零结果需要"明确成功 + 合法空列表"。
    """
    s = stub(total=0, omit_datas=True)
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_missing_datas_with_rows_is_parse_failure(stub):
    """声明有总数但缺 datas：属解析失败，不发布、不算零结果。"""
    s = stub(total=5, omit_datas=True)
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_retry_uses_one_response_not_a_mixture(stub, monkeypatch):
    """截断重取后，行、总数、条件必须来自同一次响应。

    构造：第一次声明 3 只、返回 2 行（被 perpage 截断）；第二次声明 2 只、返回空。
    若把第一次的行与第二次的总数拼在一起，会误判为"完整"；正确做法是整份采用第二次，
    于是"声明 2、实际 0 行"应判为不完整。
    """
    monkeypatch.setattr(session_mod, "_STREAM_PERPAGE", 2)
    s = stub(
        total=3,
        scripted=[
            {
                "code_count": 3,
                "row_count": 3,
                "dataSize": 2,
                "status_code": 0,
                "status_msg": "请求正常",
                "token": "t1",
                "model_sql": _CONDITION,
                "datas": [_row("600001"), _row("600002")],
            },
            {
                "code_count": 2,
                "row_count": 2,
                "dataSize": 0,
                "status_code": 0,
                "status_msg": "请求正常",
                "token": "t2",
                "model_sql": _CONDITION,
                "datas": [],
            },
        ],
    )
    with pytest.raises(WencaiIncomplete):
        acquire(URL, _session(s), perpage=100)


def test_missing_status_is_not_confirmed_zero_result(stub):
    """缺成功状态、声明零总数且空列表：不能确认为真实零结果（A10 要求"明确成功"）。"""
    s = stub(total=0, omit_status=True)
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_zero_total_with_rows_is_not_confirmed_zero_result(stub):
    """状态成功、声明零总数，却带回股票行：自相矛盾，不承认零结果。"""
    s = stub(
        total=0,
        scripted=[
            {
                "code_count": 0,
                "row_count": 0,
                "dataSize": 2,
                "status_code": 0,
                "status_msg": "请求正常",
                "token": "t1",
                "model_sql": _CONDITION,
                "datas": [_row("600001"), _row("600002")],
            }
        ],
    )
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_malformed_row_entry_is_not_confirmed_zero_result(stub):
    """行列表含非对象元素：结构非法属解析失败，不能因过滤后为空而确认为零结果。

    回归：_rows 曾静默过滤非 dict 元素，使 ["broken"] 变成空列表，
    绕过"零结果需明确成功且空列表"的校验。
    """
    s = stub(
        total=0,
        scripted=[
            {
                "code_count": 0,
                "row_count": 0,
                "dataSize": 1,
                "status_code": 0,
                "status_msg": "请求正常",
                "token": "t1",
                "model_sql": _CONDITION,
                "datas": ["broken"],
            }
        ],
    )
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_malformed_row_among_valid_rows_is_parse_failure(stub):
    """合法行中混入非法元素同样拒绝，不按"取合法行、丢异常行"处理。"""
    s = stub(
        total=2,
        scripted=[
            {
                "code_count": 2,
                "row_count": 2,
                "dataSize": 2,
                "status_code": 0,
                "status_msg": "请求正常",
                "token": "t1",
                "model_sql": _CONDITION,
                "datas": [_row("600001"), "broken"],
            }
        ],
    )
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)


def test_stream_session_business_error_is_unexpected(stub):
    """status_code 非 0 属业务异常，不解读为零结果。"""
    s = stub(total=3, business_status=-1)
    with pytest.raises(WencaiUnexpected):
        acquire(URL, _session(s), perpage=100)
