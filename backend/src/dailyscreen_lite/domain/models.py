"""领域模型：来源、批次、证券、候选项与处理状态。

字段只覆盖已确认行为，不提前引入退市库、撤销栈或多套行情。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import NotRequired, TypedDict


class SourceKind(str, Enum):
    """导入来源类型。文件、文本与链接均已实现，各自解析器与获取渠道分离。"""

    CSV = "csv"
    XLSX = "xlsx"
    TEXT = "text"
    LINK = "link"


class BatchStatus(str, Enum):
    """批次状态。异常批次保留并展示原因，只有 PUBLISHED 才产生候选项。

    AWAITING_SELECTION 与 AWAITING_CONFIRMATION 是待处理的非终态：等待用户选择
    代码列/工作表，或确认问财解析条件；它们不阻塞其他来源批次。
    """

    PUBLISHED = "published"
    EMPTY = "empty"
    ALL_UNKNOWN = "all_unknown"
    REJECTED = "rejected"
    AWAITING_SELECTION = "awaiting_selection"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


PENDING_STATUSES = frozenset(
    {BatchStatus.AWAITING_SELECTION, BatchStatus.AWAITING_CONFIRMATION}
)


class StockOutcome(str, Enum):
    """批次内单条记录的处置结果。"""

    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"


class CandidateState(str, Enum):
    """候选项处理状态：未处理（待归类/稍后处理）与三类已处理终态。"""

    PENDING = "pending"
    LATER = "later"
    DISMISSED = "dismissed"
    OBSERVED = "observed"
    CLEARED = "cleared"


UNPROCESSED_STATES = frozenset({CandidateState.PENDING, CandidateState.LATER})


class CandidateScope(str, Enum):
    """候选归类视图范围：待归类含未查看、已查看未决策与稍后处理，已处理含其余终态。"""

    UNPROCESSED = "unprocessed"
    PROCESSED = "processed"

    @property
    def states(self) -> tuple[CandidateState, ...]:
        if self is CandidateScope.PROCESSED:
            return (CandidateState.DISMISSED, CandidateState.OBSERVED, CandidateState.CLEARED)
        return tuple(UNPROCESSED_STATES)


@dataclass(frozen=True)
class Security:
    """权威证券库中的一条证券身份。"""

    security_id: str
    code: str
    exchange: str
    board: str
    name: str
    listing_date: str | None
    is_st: bool


@dataclass(frozen=True)
class ParsedCandidate:
    """解析出的单条股票标识候选，保留字符串前导零与原始位置。"""

    raw_code: str
    position: str
    normalized_code: str
    raw_extras: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParseResult:
    """一次来源解析的完整结果。

    declared_total 为来源声明的记录总数，未知时保持 None，不伪造。
    """

    candidates: tuple[ParsedCandidate, ...]
    declared_total: int | None
    structure: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportDate:
    """导入日期：北京时间日期，接收提交时固定。"""

    value: date

    @property
    def iso(self) -> str:
        return self.value.isoformat()


@dataclass(frozen=True)
class SelectionOption:
    """歧义选择的一个候选项。value 是回传值，label 是展示文本。"""

    value: str
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class SelectionPreview:
    """歧义选择的最小预览：表头与少量样例行，供用户确认列语义。"""

    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SelectionRequest:
    """需要用户确认结构时返回的轻量选择请求。"""

    kind: str  # "sheet" | "column"
    prompt: str
    options: tuple[SelectionOption, ...]
    preview: SelectionPreview = SelectionPreview()

    def to_dict(self) -> dict:
        """持久化与 API 响应共用的可序列化形式。"""
        return {
            "kind": self.kind,
            "prompt": self.prompt,
            "options": [
                {"value": o.value, "label": o.label, "detail": o.detail}
                for o in self.options
            ],
            "preview": {
                "headers": list(self.preview.headers),
                "rows": [list(r) for r in self.preview.rows],
            },
        }


@dataclass(frozen=True)
class ParseOptions:
    """解析上下文：用户在歧义选择中已确认的工作表与代码列（0 起）。"""

    sheet: str | None = None
    code_column: int | None = None


@dataclass(frozen=True)
class BatchStock:
    """批次内单条股票记录，保留原始位置以便追溯。

    raw_extras 为该行来源附带字段的最小证据存档，只读，不参与证券识别或行情计算。
    effect 记录该行对候选项队列的影响（新增/合并/重新归类/仅追加来源），供结果表展示。
    """

    position: str
    raw_code: str
    normalized_code: str
    outcome: StockOutcome
    security_id: str | None
    reason: str | None
    raw_extras: dict[str, str] = field(default_factory=dict)
    effect: str | None = None


@dataclass
class ImportBatch:
    """一次导入的来源快照与统计。"""

    batch_id: str
    source_kind: SourceKind
    source_name: str
    source_ref: str | None
    archive_path: str | None
    received_at: datetime
    import_date: ImportDate
    status: BatchStatus
    declared_total: int | None
    parsed_count: int
    unique_count: int
    recognized_count: int
    skipped_count: int
    new_candidate_count: int
    merged_candidate_count: int
    reopened_candidate_count: int
    error_code: str | None
    error_message: str | None
    stocks: tuple[BatchStock, ...] = ()
    # 问财链接来源：原句、实际解析条件与查询身份；其余来源为 None
    query_text: str | None = None
    condition_labels: tuple[str, ...] = ()
    condition_fingerprint: str | None = None
    identity_fingerprint: str | None = None
    # 获取完整性结论（与 wencai.acquisition 的常量取值一致，如 internally_consistent）
    completeness: str | None = None
    # 待选择时返回给用户的歧义请求
    selection: SelectionRequest | None = None
    # 跳过项重新识别的结果
    reidentified_at: datetime | None = None
    reidentified_imported: int = 0
    reidentified_removed: int = 0


@dataclass(frozen=True)
class CandidateSelection:
    """一次每日入选：某股票在某个北京时间导入日期首次成功发布的事实。

    来源只作追溯：同日的其他来源只追加 CandidateSource，不产生第二条入选。
    """

    import_date: ImportDate
    batch_id: str
    selected_at: datetime


@dataclass(frozen=True)
class CandidateHistoryEntry:
    """历次处理记录：动作、状态迁移与时间，供「来源与处理记录」展开阅读。"""

    action: str
    from_state: CandidateState | None
    to_state: CandidateState
    acted_at: datetime
    detail: str | None = None


@dataclass
class Candidate:
    """候选项：一只股票当前待完成的归类对象，同一股票最多一个未处理对象。

    未处理期间跨日再次入选合并到同一候选项，保留各次入选日期与来源；处理完成后
    另一个导入日期的首次有效入选使同一候选项重新进入待归类，并保留历次处理记录。
    队列顺序由数据库的 queue_order 列维护，不进入领域对象。
    """

    candidate_id: str
    security_id: str
    state: CandidateState
    first_seen_at: datetime
    last_action_at: datetime | None
    action_result: str | None
    viewed_at: datetime | None = None
    security: Security | None = None
    selections: tuple[CandidateSelection, ...] = ()
    # 全部来源批次（含同日只追加证据、未产生新入选的批次）
    sources: tuple[str, ...] = ()
    history: tuple[CandidateHistoryEntry, ...] = ()

    @property
    def import_dates(self) -> tuple[ImportDate, ...]:
        """全部入选日期，升序；日期筛选与卡片展示共用同一集合。"""
        return tuple(sorted((s.import_date for s in self.selections), key=lambda d: d.value))

    @property
    def latest_import_date(self) -> ImportDate | None:
        dates = self.import_dates
        return dates[-1] if dates else None


@dataclass(frozen=True)
class CandidateRow:
    """候选列表的一行轻量投影：只含左侧列表与卡片归属所需的字段。

    列表读取不装配来源明细、笔记内容或完整处理历史；当前卡的详细资料按当前
    候选项单独读取，因此列表可以批量取回而不做逐项详细查询。
    """

    candidate_id: str
    security_id: str
    state: CandidateState
    viewed_at: datetime | None
    latest_import_date: str | None
    source_count: int
    observed: bool
    security: Security | None = None


@dataclass(frozen=True)
class Note:
    """个股笔记：归属单只股票、跨导入日期共用。

    只保留当前正文与最近保存时间，不保留修订历史；删除即删除。
    """

    note_id: str
    security_id: str
    body: str
    updated_at: datetime


@dataclass(frozen=True)
class DailyBar:
    """单只股票某一交易日的日线（默认前复权口径）。

    volume_lots 为「手」（与 AKShare 一致），amount_yuan 为「元」；两者单位在
    接入层统一，界面据此标注，不用零值伪造缺失。
    """

    security_id: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume_lots: float
    amount_yuan: float | None


@dataclass(frozen=True)
class QuoteSeries:
    """某股票的日线序列：口径、实际来源与取数时间随数据记录。"""

    security_id: str
    adjust: str
    source: str
    bars: tuple[DailyBar, ...]
    fetched_at: datetime


class SuspensionKind(str, Enum):
    """来源给出的停牌期限：连续停牌是整天无交易，盘中停牌当天仍有成交。"""

    CONTINUOUS = "continuous"  # 连续停牌：可豁免目标交易日的日线要求
    INTRADAY = "intraday"  # 盘中停牌：当天部分时段交易，不豁免
    OTHER = "other"  # 来源出现其它取值时如实记录，按不豁免处理


class MarketStatus(str, Enum):
    """单只股票在某交易日的状态：正常交易、确认全天停牌、未知。

    未知包括来源未覆盖的市场（本次探查未见北交所行）与状态来源不可用两种情况，
    都不当作停牌，也不据此宣称全市场已验证。
    """

    TRADING = "trading"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Suspension:
    """一条停牌区间记录（来源：AKShare stock_tfp_em）。

    起止日期是判定某日是否全天停牌的权威依据：接口按「当前未复牌」返回，
    不能按是否出现在某一日的列表里判停牌，故区间缺失时该条不参与豁免。
    """

    security_id: str
    code: str
    name: str
    kind: SuspensionKind
    start_date: date
    end_date: date | None
    expected_resume: date | None
    market: str
    reason: str | None

    def covers(self, day: date) -> bool:
        """该停牌区间是否覆盖 day；截止日期未定时按「尚未复牌」处理。"""
        if day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date


@dataclass(frozen=True)
class MarketStatusSnapshot:
    """每日全市场状态快照：目标交易日、来源、采集时间与停牌区间。

    只保存停牌区间与覆盖声明，不逐只落「正常」，因此是轻量快照；
    来源未覆盖的市场必须显式声明，不能把未知当作正常。
    """

    snapshot_id: str
    target_trade_date: date
    collected_at: datetime
    source: str
    calendar_source: str | None
    covered_markets: tuple[str, ...]
    uncovered_markets: tuple[str, ...]
    suspensions: tuple[Suspension, ...]
    status: str  # "ok" | "failed"
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class StockDataState(str, Enum):
    """业务页面上的逐股数据状态，对应数据中心与顶部提醒。"""

    UPDATED = "updated"  # 已取得目标交易日完整日线
    SUSPENDED = "suspended"  # 确认全天停牌，当日无新日线属正常
    PENDING = "pending"  # 状态正常但目标日行情缺失：待补齐
    UNCONFIRMED = "unconfirmed"  # 状态无法确认：未补齐，状态待确认


@dataclass(frozen=True)
class StockDataStatus:
    """一只股票的完整性结论：状态、最后行情日期与原因。"""

    security_id: str
    name: str
    state: StockDataState
    market_status: MarketStatus
    latest_date: str | None
    reason: str | None
    last_error: str | None = None
    source: str | None = None


class UpdateKind(str, Enum):
    """更新触发来源：复用同一流程，仅用于区分调度语义与展示。"""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    STARTUP = "startup"
    IMPORT = "import"
    RETRY = "retry"
    SINGLE = "single"


class UpdateStatus(str, Enum):
    """一次更新的终态。RUNNING 是进行中的非终态。"""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


# 一次更新里证券库步骤的结果：成功替换、失败保留旧库、本次不动
SECURITIES_OK = "ok"
SECURITIES_FAILED = "failed"
SECURITIES_SKIPPED = "skipped"


@dataclass
class UpdateRun:
    """一次数据更新的记录：证券库步骤与行情步骤的成功/失败/跳过分别如实记录。"""

    run_id: str
    kind: UpdateKind
    status: UpdateStatus
    started_at: datetime
    finished_at: datetime | None
    securities_status: str | None
    securities_message: str | None
    securities_count: int
    quotes_ok: int
    quotes_failed: int
    # 真实无数据（新上市/停牌等）既非成功也非失败，单独计数，不误报为失败
    quotes_skipped: int = 0
    # 请求成功但目标交易日行情仍未补齐的数量：不因抓取成功就把这次更新说成全部完成
    quotes_pending: int = 0
    failed_securities: tuple[str, ...] = ()
    elapsed_ms: int | None = None

    @property
    def securities_failed(self) -> bool:
        """证券库步骤是否失败：失败时界面单独提示「使用上次数据」。"""
        return self.securities_status == SECURITIES_FAILED


class UpdateItemStatus(str, Enum):
    """逐股更新结果：成功、获取失败（保留旧数据）、来源成功但无数据。"""

    OK = "ok"
    FAILED = "failed"
    NO_DATA = "no_data"


class SourceAttempt(TypedDict):
    """One provider request as persisted in update diagnostics."""

    source: str
    elapsedMs: int
    outcome: str
    latestDate: str | None
    error: NotRequired[str]


@dataclass(frozen=True)
class UpdateItemResult:
    """一次更新中某只股票的结果，供数据中心逐股展示失败原因与日期。"""

    security_id: str
    status: UpdateItemStatus
    message: str | None
    trade_date: str | None
    fetch_mode: str | None = None
    request_start: str | None = None
    elapsed_ms: int | None = None
    attempts: tuple[SourceAttempt, ...] = ()


DEFAULT_OBSERVATION_GROUP_ID = "default"
DEFAULT_OBSERVATION_GROUP_NAME = "默认观察组"
# 默认组是否已经种下：删除后重启不得重建，因此用一次性种子标记而不是「组是否存在」
SEED_DEFAULT_OBSERVATION_GROUP = "default_observation_group"


@dataclass(frozen=True)
class ObservationGroup:
    """观察组：股票长期关注分类。

    只有创建、重命名与删除，没有归档；删除只移除该组关系，
    股票在其他组的关系、笔记、来源与处理历史都不受影响。
    """

    group_id: str
    name: str
    is_default: bool
    created_at: datetime
    # 组内成员数量；读取列表时统计，不参与身份判断
    member_count: int = 0


@dataclass(frozen=True)
class ObservedStock:
    """观察列表中的一只股票：按证券去重，带全部现存组与加入时间。

    加入时间取该股票在这些组中最早的加入时刻，用于默认的倒序排列；
    切到单组时取该组内的加入时刻。security 为权威证券库身份，用于列表显示名称。
    """

    security_id: str
    group_ids: tuple[str, ...]
    joined_at: datetime
    security: Security | None = None


# 观察列表排序：加入时间倒序、名称、涨跌幅
OBSERVATION_SORTS = frozenset({"joined", "name", "change"})
DEFAULT_OBSERVATION_SORT = "joined"


@dataclass
class ObservationState:
    """观察组模块的浏览上下文：当前组、排序与当前股票。

    与候选归类各自独立保存，模块间不自动同步当前股票；
    显式搜索或跨模块动作才更新目标。
    """

    group_id: str | None = None
    sort: str = DEFAULT_OBSERVATION_SORT
    current_security_id: str | None = None


VIEW_MODES = frozenset({"card", "list"})
DEFAULT_VIEW_MODE = "card"

# 浏览路径里的结束卡步骤：路径步骤要么是候选项标识，要么是这个标记。
# 结束卡与候选项走同一套前后移动规则，因此它也是路径中的一步（可回看、可跨重启恢复）；
# 该标识不是证券代码形态，不会与候选项标识冲突。
END_STEP = "~end"


@dataclass
class ClassificationState:
    """候选归类浏览上下文：当前候选项、视图与筛选，跨刷新与重启恢复。

    浏览位置与业务处理状态分开保存，因此重新进入不会跳回刚浏览过的队首未决策项。
    path 是本轮按访问顺序保存的浏览路径，每一步是候选卡或结束卡
    （`END_STEP`），允许同一候选多次出现；cursor 指向当前步骤。
    手动聚焦从游标处截断前进分支再追加目标，历史中途跳转因此替换掉原前进分支。
    round_started_at 标记本轮起点，用于结束卡统计真实浏览与处理数量。
    """

    current_candidate_id: str | None = None
    view_mode: str = DEFAULT_VIEW_MODE
    scope: CandidateScope = CandidateScope.UNPROCESSED
    import_date: str | None = None
    search: str = ""
    result: CandidateState | None = None
    round_started_at: datetime | None = None
    # scope 跟随当前卡真实状态；filter_scope 保留本轮寻找新股票的范围。
    filter_scope: CandidateScope = CandidateScope.UNPROCESSED
    path: tuple[str, ...] = ()
    cursor: int = -1
    # 是否停在结束卡：由当前步骤是否 END_STEP 导出，不单独保存，
    # 避免路径与状态各说一套。列 ended 仍写回当前值，供旧库与人工排查。
    ended: bool = False
    navigation_revision: int = 0
    # 候选列表版本：任何改变列表成员、顺序或列表可见字段的写入都必须推进它。
    # 前端据此判断本地列表是否过期，不自行复制筛选规则。
    list_revision: int = 0

    @property
    def current_step(self) -> str | None:
        """当前步骤标识；路径为空或游标越界时为 None。"""
        if not self.path or self.cursor < 0 or self.cursor >= len(self.path):
            return None
        return self.path[self.cursor]

    @property
    def at_end_step(self) -> bool:
        """当前是否停在结束卡。"""
        return self.current_step == END_STEP
