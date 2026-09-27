"""SQLite 连接与模式管理。

采用 WAL 与外键约束；每次连接独立，事务由调用方通过 with 语句控制。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
create table if not exists automatic_rounds (
    run_date text not null, slot integer not null,
    run_id text not null, started_at text not null,
    primary key (run_date, slot)
);
create table if not exists calendar_months (
    month text primary key, source text not null, updated_at text not null
);
create table if not exists calendar_update (
    id integer primary key check(id = 1), status text not null,
    message text not null, attempted_at text not null
);
create table if not exists securities_market_updates (
    exchange text primary key,
    status text not null,
    message text not null,
    source text not null,
    attempted_at text not null,
    succeeded_at text
);

create table if not exists securities (
    security_id     text primary key,
    code            text not null,
    exchange        text not null,
    board           text not null,
    name            text not null default '',
    listing_date    text,
    is_st           integer not null default 0,
    snapshot_id     text not null
);

create index if not exists idx_securities_code on securities(code);

create table if not exists securities_snapshot_meta (
    snapshot_id        text primary key,
    loaded_at          text not null,
    record_count       integer not null,
    identity_fingerprint text not null,
    effective_date     text,
    source_json        text not null
);

create table if not exists import_batches (
    batch_id            text primary key,
    source_kind         text not null,
    source_name         text not null,
    source_ref          text,
    archive_path        text,
    received_at         text not null,
    import_date         text not null,
    status              text not null,
    declared_total      integer,
    parsed_count        integer not null default 0,
    unique_count        integer not null default 0,
    recognized_count    integer not null default 0,
    skipped_count       integer not null default 0,
    new_candidate_count      integer not null default 0,
    merged_candidate_count   integer not null default 0,
    reopened_candidate_count integer not null default 0,
    error_code          text,
    error_message       text,
    query_text          text,
    condition_labels    text,
    condition_fingerprint text,
    identity_fingerprint text,
    completeness        text,
    selection           text,
    reidentified_at     text,
    reidentified_imported integer not null default 0,
    reidentified_removed  integer not null default 0,
    created_at          text not null
);

create index if not exists idx_batches_import_date on import_batches(import_date);

create table if not exists batch_stocks (
    id              integer primary key autoincrement,
    batch_id        text not null references import_batches(batch_id) on delete cascade,
    position        text not null,
    raw_code        text not null,
    normalized_code text not null,
    outcome         text not null,
    security_id     text,
    reason          text,
    raw_extras      text,
    candidate_effect text
);

create index if not exists idx_batch_stocks_batch on batch_stocks(batch_id);

-- 待确认问财批次的候选快照：确认后据此发布，不重复请求来源
create table if not exists batch_candidates (
    id              integer primary key autoincrement,
    batch_id        text not null references import_batches(batch_id) on delete cascade,
    position        text not null,
    raw_code        text not null,
    normalized_code text not null,
    raw_extras      text
);

create index if not exists idx_batch_candidates_batch on batch_candidates(batch_id);

-- 已确认的问财查询身份：同一查询且口径未变时自动发布
create table if not exists wencai_confirmations (
    query_fingerprint     text primary key,
    query_text            text not null,
    condition_fingerprint text,
    confirmed_at          text not null
);

-- 候选项：一只股票最多一个候选项，同一股票唯一；重新归类复用同一行并保留历史
create table if not exists candidates (
    candidate_id     text primary key,
    security_id      text not null unique,
    state            text not null,
    first_seen_at    text not null,
    last_action_at   text,
    action_result    text,
    viewed_at        text,
    queue_order      integer not null default 0,
    created_at       text not null
);

create index if not exists idx_candidates_state on candidates(state);
create index if not exists idx_candidates_queue on candidates(queue_order);

-- 每日入选：证券身份＋北京时间导入日期唯一；同日其他来源只追加来源，不新增入选
create table if not exists candidate_selections (
    candidate_id text not null references candidates(candidate_id) on delete cascade,
    import_date  text not null,
    batch_id     text not null references import_batches(batch_id) on delete cascade,
    selected_at  text not null,
    primary key (candidate_id, import_date)
);

create index if not exists idx_candidate_selections_date
    on candidate_selections(import_date);
create index if not exists idx_candidate_selections_batch
    on candidate_selections(batch_id);

-- 候选项的全部来源批次（含只追加证据、未产生新入选的同日来源）
create table if not exists candidate_sources (
    candidate_id text not null references candidates(candidate_id) on delete cascade,
    batch_id     text not null references import_batches(batch_id) on delete cascade,
    import_date  text not null,
    created_at   text not null,
    primary key (candidate_id, batch_id)
);

create index if not exists idx_candidate_sources_batch on candidate_sources(batch_id);

-- 历次处理记录：重新归类保留历史，不把过去处理变成当前卡片状态
create table if not exists candidate_history (
    id           integer primary key autoincrement,
    candidate_id text not null references candidates(candidate_id) on delete cascade,
    action       text not null,
    from_state   text,
    to_state     text not null,
    acted_at     text not null,
    detail       text
);

create index if not exists idx_candidate_history_candidate
    on candidate_history(candidate_id, acted_at desc);

-- 候选归类浏览上下文：当前项、视图模式与筛选，跨刷新与重启恢复
create table if not exists classification_state (
    id                   integer primary key check (id = 1),
    current_candidate_id text,
    view_mode            text not null default 'card',
    scope                text not null default 'unprocessed',
    import_date          text,
    search               text not null default '',
    result               text,
    round_started_at     text,
    filter_scope         text not null default 'unprocessed',
    path_json            text not null default '[]',
    cursor               integer not null default -1,
    ended                integer not null default 0,
    navigation_revision  integer not null default 0,
    list_revision        integer not null default 0,
    updated_at           text not null
);

-- 观察组：默认组随应用首次启动种下；只有删除，没有归档
create table if not exists observation_groups (
    group_id   text primary key,
    name       text not null,
    is_default integer not null default 0,
    created_at text not null,
    updated_at text not null
);

-- 组名唯一，避免用户建出两个同名分类；删除后该名字重新可用
create unique index if not exists idx_observation_groups_name
    on observation_groups(name);

-- 多组成员关系：同股可在多个组，重复保存幂等
create table if not exists observation_members (
    group_id    text not null references observation_groups(group_id) on delete cascade,
    security_id text not null,
    created_at  text not null,
    primary key (group_id, security_id)
);

create index if not exists idx_observation_members_security
    on observation_members(security_id);

-- 一次性种子标记：默认组被删除后重启不得重建，因此记「已种下」而不是「是否存在」
create table if not exists app_seeds (
    seed_key   text primary key,
    created_at text not null
);

-- 观察组模块浏览上下文：当前组、排序与当前股票，跨刷新与重启恢复
create table if not exists observation_state (
    id                  integer primary key check (id = 1),
    group_id            text,
    sort                text not null default 'joined',
    current_security_id text,
    updated_at          text not null
);

-- 个股笔记：按证券标识归属、跨导入日期共用；只留正文与最近保存时间
create table if not exists notes (
    note_id     text primary key,
    security_id text not null,
    body        text not null,
    updated_at  text not null
);

create index if not exists idx_notes_security on notes(security_id, updated_at desc);

-- 日线行情：主键含复权口径，便于整段替换而不与新口径混杂
create table if not exists quote_history_archive (
    security_id text not null, adjust text not null, digest text not null,
    archived_at text not null, payload text not null,
    primary key(security_id, adjust, digest)
);

create table if not exists daily_quotes (
    security_id text not null,
    trade_date  text not null,
    adjust      text not null,
    open        real not null,
    high        real not null,
    low         real not null,
    close       real not null,
    volume_lots real not null,
    amount_yuan real,
    source      text not null,
    fetched_at  text not null,
    primary key (security_id, trade_date, adjust)
);

create index if not exists idx_daily_quotes_lookup
    on daily_quotes(security_id, adjust, trade_date desc);

-- 数据更新记录：证券库与行情两步分别记录成功/失败，重启后可读
create table if not exists update_runs (
    run_id            text primary key,
    kind              text not null,
    status            text not null,
    started_at        text not null,
    finished_at       text,
    securities_status text,
    securities_message text,
    securities_count  integer not null default 0,
    quotes_ok         integer not null default 0,
    quotes_failed     integer not null default 0,
    quotes_skipped    integer not null default 0,
    quotes_pending    integer not null default 0,
    failed_securities text,
    elapsed_ms        integer
);

create index if not exists idx_update_runs_started on update_runs(started_at desc);

-- 逐股更新结果：数据中心逐股展示失败原因与日期，重启后仍可读
create table if not exists update_stock_results (
    run_id      text not null references update_runs(run_id) on delete cascade,
    security_id text not null,
    status      text not null,
    message     text,
    trade_date  text,
    fetch_mode  text,
    request_start text,
    elapsed_ms  integer,
    attempts_json text,
    primary key (run_id, security_id)
);

-- 每日全市场状态快照：目标交易日、来源、采集时间与覆盖声明
create table if not exists market_status_snapshots (
    snapshot_id       text primary key,
    target_trade_date text not null,
    collected_at      text not null,
    source            text not null,
    calendar_source   text,
    covered_markets   text not null,
    uncovered_markets text not null,
    suspension_count  integer not null default 0,
    status            text not null,
    message           text
);

create index if not exists idx_market_status_collected
    on market_status_snapshots(collected_at desc);

-- 停牌区间：判定某日是否全天停牌的依据，不用「是否出现在该日列表」判断
create table if not exists market_suspensions (
    snapshot_id     text not null references market_status_snapshots(snapshot_id) on delete cascade,
    security_id     text not null,
    code            text not null,
    name            text not null default '',
    kind            text not null,
    start_date      text not null,
    end_date        text,
    expected_resume text,
    market          text not null default '',
    reason          text,
    primary key (snapshot_id, security_id, start_date)
);

create index if not exists idx_market_suspensions_security
    on market_suspensions(security_id);

-- 交易日历缓存：状态来源失败时仍能算目标交易日，不把周末节假日当作缺当日日线
create table if not exists trade_dates (
    trade_date text primary key,
    source     text not null,
    updated_at text not null
);
"""


class Database:
    """轻量 SQLite 访问入口。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma foreign_keys=on")
        conn.execute("pragma busy_timeout=5000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：异常时整体回滚，不留下部分发布。"""
        conn = self.connect()
        try:
            conn.execute("begin immediate")
            yield conn
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """单条语句或互不相关的读取。

        SQLite 在 autocommit 下每条语句各成一个快照，因此需要「同一时刻的多条读取」
        （例如浏览结果的当前卡＋列表＋数量）请用 `read_view()`。
        """
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read_view(self) -> Iterator[sqlite3.Connection]:
        """一次读取视图：多条查询落在同一个数据库快照上。

        WAL 下 `begin` 取的是延迟读事务，读到的是一致快照且不阻塞写入。
        """
        conn = self.connect()
        try:
            conn.execute("begin")
            yield conn
        finally:
            conn.execute("rollback")
            conn.close()

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """补齐旧库列；成交额可空通过单表原子复制迁移，不引入迁移框架。

        本轮候选项重建不迁移旧业务数据：旧库中的 research_items 等遗留表原样保留、
        不再读写，新库从 candidates 系列表开始。旧运行数据清理留到首次交付切换。
        观察组归档移除后，旧库残留的 observation_groups.archived 列与索引不再使用，
        也不做破坏性删除，新建库不再包含它们。
        """
        amount = next(r for r in conn.execute("pragma table_info(daily_quotes)") if r["name"] == "amount_yuan")
        if amount["notnull"]:
            # Rebuild only this table, atomically; every existing quote is copied.
            conn.execute("begin immediate")
            try:
                definition = conn.execute("select sql from sqlite_master where name = 'daily_quotes'").fetchone()[0]
                import re
                definition = re.sub(r"(?i)amount_yuan\s+real\s+not\s+null", "amount_yuan real", definition)
                definition = definition.replace("daily_quotes", "daily_quotes_nullable", 1)
                conn.execute(definition)
                conn.execute("insert into daily_quotes_nullable select * from daily_quotes")
                conn.execute("drop table daily_quotes")
                conn.execute("alter table daily_quotes_nullable rename to daily_quotes")
                conn.execute("create index idx_daily_quotes_lookup on daily_quotes(security_id, adjust, trade_date desc)")
                conn.execute("commit")
            except Exception:
                conn.execute("rollback")
                raise
        _ensure_columns(
            conn,
            "batch_stocks",
            {"raw_extras": "text", "candidate_effect": "text"},
        )
        _ensure_columns(
            conn,
            "import_batches",
            {
                "query_text": "text",
                "condition_labels": "text",
                "condition_fingerprint": "text",
                "identity_fingerprint": "text",
                "completeness": "text",
                "selection": "text",
                "reidentified_at": "text",
                "reidentified_imported": "integer not null default 0",
                "reidentified_removed": "integer not null default 0",
                "new_candidate_count": "integer not null default 0",
                "merged_candidate_count": "integer not null default 0",
                "reopened_candidate_count": "integer not null default 0",
            },
        )
        # 旧库更新记录缺行情跳过与「未补齐」计数（工单 06 与复核修复追加）
        _ensure_columns(
            conn,
            "update_runs",
            {
                "quotes_skipped": "integer not null default 0",
                "quotes_pending": "integer not null default 0",
                "elapsed_ms": "integer",
            },
        )
        _ensure_columns(
            conn,
            "update_stock_results",
            {"fetch_mode": "text", "request_start": "text", "elapsed_ms": "integer",
             "attempts_json": "text"},
        )
        added = _ensure_columns(
            conn,
            "classification_state",
            {
                "filter_scope": "text not null default 'unprocessed'",
                "path_json": "text not null default '[]'",
                "cursor": "integer not null default -1",
                "ended": "integer not null default 0",
                "navigation_revision": "integer not null default 0",
                "list_revision": "integer not null default 0",
            },
        )
        if "filter_scope" in added:
            conn.execute("update classification_state set filter_scope = scope")


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> set[str]:
    existing = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    added: set[str] = set()
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"alter table {table} add column {name} {definition}")
            added.add(name)
    return added
