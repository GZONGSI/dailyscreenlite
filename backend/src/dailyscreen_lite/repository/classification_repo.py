"""候选项、每日入选、来源与处理记录的持久化读写。

队列顺序由 queue_order 决定：新建候选项排到队尾，稍后处理与处理后的新入选同样
移到队尾，主动重新归类把目标调到队首，其余项相对顺序不变。搜索与列表点选不改变
队列顺序（打开候选只是浏览，不是重新排队）。
viewed_at 与 state 分离，用于区分未查看/已查看未决策，不改变处理状态；
「本轮是否看过」以浏览路径上的步骤为准（viewed_at 跨轮保留，不代表本轮）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from dailyscreen_lite.domain.models import (
    Candidate,
    CandidateHistoryEntry,
    CandidateRow,
    CandidateScope,
    CandidateSelection,
    CandidateState,
    ClassificationState,
    ImportDate,
    Security,
)

_COLUMNS = """
    candidate_id, security_id, state, first_seen_at,
    last_action_at, action_result, viewed_at, queue_order
"""

_COLUMN_NAMES = tuple(name.strip() for name in _COLUMNS.replace("\n", " ").split(","))
_QUALIFIED_COLUMNS = ", ".join(f"c.{name}" for name in _COLUMN_NAMES)

# 轻量列表投影：只取渲染左侧列表与卡片归属所需的列，不装配来源明细、笔记内容
# 与完整处理历史（这些只属于当前卡的详细资料）。
# 最近入选日期与来源数用按候选命中的子查询：两者都走候选索引，
# 因此整份列表只是一次扫描；先聚合 4 百多万条入选再连接会退化成全表聚合。
_ROW_EXPRESSIONS = """
    c.candidate_id, c.security_id, c.state, c.viewed_at,
    (select max(sel.import_date) from candidate_selections sel
      where sel.candidate_id = c.candidate_id) as latest_import_date,
    (select count(*) from candidate_sources src
      where src.candidate_id = c.candidate_id) as source_count,
    exists (select 1 from observation_members mem
      where mem.security_id = c.security_id) as observed"""

# 队列按 queue_order 先进先出（同批共用序号，按证券身份稳定排序）。
_ORDER = "order by c.queue_order asc, c.security_id asc"


def _to_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        candidate_id=row["candidate_id"],
        security_id=row["security_id"],
        state=CandidateState(row["state"]),
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_action_at=(
            datetime.fromisoformat(row["last_action_at"])
            if row["last_action_at"]
            else None
        ),
        action_result=row["action_result"],
        viewed_at=datetime.fromisoformat(row["viewed_at"]) if row["viewed_at"] else None,
    )


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --- 候选项 ---


def insert_candidate(
    conn: sqlite3.Connection, candidate: Candidate, *, queue_order: int
) -> None:
    """插入候选项。queue_order 由调用方按批次分配，同批共用序号以保持稳定顺序。"""
    conn.execute(
        f"""
        insert into candidates ({_COLUMNS}, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate.candidate_id,
            candidate.security_id,
            candidate.state.value,
            candidate.first_seen_at.isoformat(),
            candidate.last_action_at.isoformat() if candidate.last_action_at else None,
            candidate.action_result,
            candidate.viewed_at.isoformat() if candidate.viewed_at else None,
            queue_order,
            candidate.first_seen_at.isoformat(),
        ),
    )


def find_by_security(
    conn: sqlite3.Connection, security_id: str
) -> Candidate | None:
    row = conn.execute(
        f"select {_COLUMNS} from candidates where security_id = ?", (security_id,)
    ).fetchone()
    return _to_candidate(row) if row else None


def existing_candidate_ids(
    conn: sqlite3.Connection, candidate_ids: tuple[str, ...]
) -> set[str]:
    """这些标识里仍然存在的候选项（一次查询完成，供浏览路径整体核对）。"""
    if not candidate_ids:
        return set()
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"select candidate_id from candidates where candidate_id in ({placeholders})",
        list(candidate_ids),
    ).fetchall()
    return {row["candidate_id"] for row in rows}


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> Candidate | None:
    row = conn.execute(
        f"select {_COLUMNS} from candidates where candidate_id = ?", (candidate_id,)
    ).fetchone()
    return _to_candidate(row) if row else None


def _list_filters(
    states: tuple[CandidateState, ...] | None,
    import_date: str | None,
    search: str | None,
    result: CandidateState | None,
) -> tuple[str, list[str]]:
    """把范围、日期、搜索与结果筛选编译为 where 子句与参数。

    日期筛选匹配候选项的全部入选日期：跨日合并的候选项在任一天筛选中都指向
    同一个对象；处理一次后各日期视图同步更新。
    """
    if result is not None:
        if states is None:
            states = (result,)
        else:
            # 结果筛选只在当前范围内生效，避免"已处理"的结果串到"待归类"范围
            states = tuple(s for s in states if s is result)
        if not states:
            return "where 0", []
    clauses: list[str] = []
    params: list[str] = []
    if states:
        clauses.append(f"c.state in ({','.join('?' for _ in states)})")
        params.extend(s.value for s in states)
    if import_date:
        clauses.append(
            "exists (select 1 from candidate_selections sel "
            "where sel.candidate_id = c.candidate_id and sel.import_date = ?)"
        )
        params.append(import_date)
    if search:
        pattern = _like_pattern(search)
        clauses.append(
            "(s.code like ? escape '\\' or s.name like ? escape '\\' "
            "or c.security_id like ? escape '\\')"
        )
        params.extend([pattern, pattern, pattern])
    return (f"where {' and '.join(clauses)}" if clauses else ""), params


def list_candidates(
    conn: sqlite3.Connection,
    states: tuple[CandidateState, ...] | None = None,
    import_date: str | None = None,
    *,
    search: str | None = None,
    result: CandidateState | None = None,
) -> list[Candidate]:
    where, params = _list_filters(states, import_date, search, result)
    rows = conn.execute(
        f"""
        select {_QUALIFIED_COLUMNS}
        from candidates c
        left join securities s on s.security_id = c.security_id
        {where}
        {_ORDER}
        """,
        params,
    ).fetchall()
    return [_to_candidate(r) for r in rows]


def _to_row(row: sqlite3.Row) -> CandidateRow:
    return CandidateRow(
        candidate_id=row["candidate_id"],
        security_id=row["security_id"],
        state=CandidateState(row["state"]),
        viewed_at=datetime.fromisoformat(row["viewed_at"]) if row["viewed_at"] else None,
        latest_import_date=row["latest_import_date"],
        source_count=row["source_count"],
        observed=bool(row["observed"]),
        security=(
            Security(
                security_id=row["security_id"],
                code=row["code"],
                exchange=row["exchange"],
                board=row["board"],
                name=row["name"],
                listing_date=row["listing_date"],
                is_st=bool(row["is_st"]),
            )
            if row["code"] is not None
            else None
        ),
    )


def list_candidate_rows(
    conn: sqlite3.Connection,
    states: tuple[CandidateState, ...] | None = None,
    import_date: str | None = None,
    *,
    search: str | None = None,
    result: CandidateState | None = None,
) -> list[CandidateRow]:
    """轻量批量列表：一次查询取回整份列表，不做逐项详细读取。"""
    where, params = _list_filters(states, import_date, search, result)
    rows = conn.execute(
        f"""
        select {_ROW_EXPRESSIONS},
               s.code, s.exchange, s.board, s.name, s.listing_date, s.is_st
        from candidates c
        left join securities s on s.security_id = c.security_id
        {where}
        {_ORDER}
        """,
        params,
    ).fetchall()
    return [_to_row(r) for r in rows]


def list_pool_order(
    conn: sqlite3.Connection, states: tuple[CandidateState, ...]
) -> tuple[str, ...]:
    """给定状态集合里的候选标识顺序，只取一列，不装配轻量行。

    排序口径与 `list_candidate_rows` 完全一致（同一个 `_ORDER`），供「这次动作有没有
    移动队列位置」的判断使用：那里只需要标识序列，装配整份行（含证券连接）是浪费。
    """
    if not states:
        return ()
    rows = conn.execute(
        f"""
        select c.candidate_id
        from candidates c
        where c.state in ({",".join("?" for _ in states)})
        {_ORDER}
        """,
        tuple(state.value for state in states),
    ).fetchall()
    return tuple(row["candidate_id"] for row in rows)


def candidate_in_filters(
    conn: sqlite3.Connection,
    candidate_id: str,
    states: tuple[CandidateState, ...] | None = None,
    import_date: str | None = None,
    *,
    search: str | None = None,
    result: CandidateState | None = None,
) -> bool:
    """该候选是否落在给定筛选里：存在性判断，不把列表装配到应用层。

    与 `list_candidate_rows` 共用 `_list_filters`，因此筛选口径只有一份。
    """
    where, params = _list_filters(states, import_date, search, result)
    clause = f"{where} and c.candidate_id = ?" if where else "where c.candidate_id = ?"
    row = conn.execute(
        f"""
        select 1
        from candidates c
        left join securities s on s.security_id = c.security_id
        {clause}
        limit 1
        """,
        (*params, candidate_id),
    ).fetchone()
    return row is not None


def candidate_row(conn: sqlite3.Connection, candidate_id: str) -> CandidateRow | None:
    row = conn.execute(
        f"""
        select {_ROW_EXPRESSIONS},
               s.code, s.exchange, s.board, s.name, s.listing_date, s.is_st
        from candidates c
        left join securities s on s.security_id = c.security_id
        where c.candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    return _to_row(row) if row else None


def pick_next_candidate_row(
    conn: sqlite3.Connection,
    states: tuple[CandidateState, ...],
    import_date: str | None,
    *,
    search: str | None = None,
    result: CandidateState | None = None,
    unprocessed_states: tuple[CandidateState, ...] | None = None,
    exclude_ids: tuple[str, ...] = (),
) -> str | None:
    """按当前筛选取下一个尚未查看的候选标识（队首方向）。

    选择完全由数据库完成：只回传一行的标识，不把整份列表取到应用层逐项寻找。
    exclude_ids 是本轮已经访问过的候选（浏览路径上的步骤）。
    """
    where, params = _list_filters(states, import_date, search, result)
    clauses: list[str] = []
    if unprocessed_states:
        clauses.append(f"c.state in ({','.join('?' for _ in unprocessed_states)})")
        params.extend(s.value for s in unprocessed_states)
    if exclude_ids:
        clauses.append(f"c.candidate_id not in ({','.join('?' for _ in exclude_ids)})")
        params.extend(exclude_ids)
    query = where
    if clauses:
        query = f"{where} and {' and '.join(clauses)}" if where else f"where {' and '.join(clauses)}"
    row = conn.execute(
        f"""
        select c.candidate_id
        from candidates c
        left join securities s on s.security_id = c.security_id
        {query}
        order by c.queue_order asc, c.security_id asc
        limit 1
        """,
        params,
    ).fetchone()
    return row["candidate_id"] if row else None


def count_candidates(conn: sqlite3.Connection, states: tuple[CandidateState, ...]) -> int:
    placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"select count(*) from candidates where state in ({placeholders})",
        [s.value for s in states],
    ).fetchone()[0]


def count_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "select state, count(*) as n from candidates group by state"
    ).fetchall()
    return {row["state"]: row["n"] for row in rows}


def update_state(
    conn: sqlite3.Connection,
    candidate_id: str,
    state: CandidateState,
    action_result: str | None,
    acted_at: datetime,
) -> None:
    conn.execute(
        "update candidates set state = ?, action_result = ?, last_action_at = ? "
        "where candidate_id = ?",
        (state.value, action_result, acted_at.isoformat(), candidate_id),
    )


def mark_viewed(conn: sqlite3.Connection, candidate_id: str, viewed_at: datetime) -> None:
    """标记为已查看。

    这不推进列表版本：已查看只改变单行的查看时间，且该行随导航响应里的列表变化
    一起下发，因此不需要让前端为它重读整份列表。改变成员或顺序的写入才推进版本。
    """
    conn.execute(
        "update candidates set viewed_at = ? where candidate_id = ?",
        (viewed_at.isoformat(), candidate_id),
    )


# --- 列表版本 ---


def bump_list_revision(conn: sqlite3.Connection) -> int:
    """推进候选列表版本；返回新版本号。

    任何改变列表成员、顺序或列表可见字段（状态、已查看时间、入选日期、来源数、
    观察关系）的写入都必须在其同一事务里调用本函数，这样前端不会看到
    「数据变了但版本没变」的中间状态。
    """
    conn.execute(
        """
        insert into classification_state (id, list_revision, updated_at)
        values (1, 0, '')
        on conflict(id) do nothing
        """
    )
    conn.execute(
        "update classification_state set list_revision = list_revision + 1 where id = 1"
    )
    return list_revision(conn)


def list_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "select list_revision from classification_state where id = 1"
    ).fetchone()
    return int(row["list_revision"]) if row else 0


# --- 队列顺序 ---


def next_queue_order(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "select coalesce(max(queue_order), 0) + 1 from candidates"
    ).fetchone()
    return int(row[0])


def move_to_tail(conn: sqlite3.Connection, candidate_id: str) -> None:
    """移到队尾：用递增序号保证不与既有项并列。"""
    conn.execute(
        "update candidates set queue_order = ? where candidate_id = ?",
        (next_queue_order(conn), candidate_id),
    )


def move_to_front(conn: sqlite3.Connection, candidate_id: str) -> None:
    """调到队首：取当前最小序号再减一，其余项相对顺序不变。"""
    row = conn.execute(
        "select coalesce(min(queue_order), 0) - 1 from candidates"
    ).fetchone()
    conn.execute(
        "update candidates set queue_order = ? where candidate_id = ?",
        (int(row[0]), candidate_id),
    )


# --- 每日入选与来源 ---


def record_selection(
    conn: sqlite3.Connection,
    candidate_id: str,
    import_date: str,
    batch_id: str,
    selected_at: str,
) -> bool:
    """记录一次每日入选；同一天已有入选时返回 False，不重复消耗当天机会。"""
    cursor = conn.execute(
        """
        insert or ignore into candidate_selections
            (candidate_id, import_date, batch_id, selected_at)
        values (?, ?, ?, ?)
        """,
        (candidate_id, import_date, batch_id, selected_at),
    )
    return cursor.rowcount > 0


def link_source(
    conn: sqlite3.Connection,
    candidate_id: str,
    batch_id: str,
    import_date: str,
    created_at: str,
) -> None:
    """幂等追加来源：同一批次重复保存不产生第二行。"""
    conn.execute(
        """
        insert or ignore into candidate_sources
            (candidate_id, batch_id, import_date, created_at)
        values (?, ?, ?, ?)
        """,
        (candidate_id, batch_id, import_date, created_at),
    )


def selections_for(
    conn: sqlite3.Connection, candidate_ids: tuple[str, ...]
) -> dict[str, list[CandidateSelection]]:
    if not candidate_ids:
        return {}
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"""
        select candidate_id, import_date, batch_id, selected_at
        from candidate_selections
        where candidate_id in ({placeholders})
        order by import_date asc, candidate_id asc
        """,
        list(candidate_ids),
    ).fetchall()
    grouped: dict[str, list[CandidateSelection]] = {}
    for row in rows:
        grouped.setdefault(row["candidate_id"], []).append(
            CandidateSelection(
                import_date=ImportDate(date.fromisoformat(row["import_date"])),
                batch_id=row["batch_id"],
                selected_at=datetime.fromisoformat(row["selected_at"]),
            )
        )
    return grouped


def sources_for(
    conn: sqlite3.Connection, candidate_ids: tuple[str, ...]
) -> dict[str, list[tuple[str, str]]]:
    """来源批次与各自导入日期，按时间升序；供卡片「来源与处理记录」展开。"""
    if not candidate_ids:
        return {}
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"""
        select candidate_id, batch_id, import_date
        from candidate_sources
        where candidate_id in ({placeholders})
        order by created_at asc, batch_id asc
        """,
        list(candidate_ids),
    ).fetchall()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["candidate_id"], []).append(
            (row["batch_id"], row["import_date"])
        )
    return grouped


# --- 历次处理记录 ---


def record_history(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    action: str,
    from_state: CandidateState | None,
    to_state: CandidateState,
    acted_at: datetime,
    detail: str | None = None,
) -> None:
    conn.execute(
        """
        insert into candidate_history
            (candidate_id, action, from_state, to_state, acted_at, detail)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            action,
            from_state.value if from_state else None,
            to_state.value,
            acted_at.isoformat(),
            detail,
        ),
    )


def history_for(
    conn: sqlite3.Connection, candidate_ids: tuple[str, ...], limit: int = 20
) -> dict[str, list[CandidateHistoryEntry]]:
    if not candidate_ids:
        return {}
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"""
        select candidate_id, action, from_state, to_state, acted_at, detail
        from candidate_history
        where candidate_id in ({placeholders})
        order by acted_at desc, id desc
        """,
        list(candidate_ids),
    ).fetchall()
    grouped: dict[str, list[CandidateHistoryEntry]] = {}
    for row in rows:
        bucket = grouped.setdefault(row["candidate_id"], [])
        if len(bucket) >= limit:
            continue
        bucket.append(
            CandidateHistoryEntry(
                action=row["action"],
                from_state=(
                    CandidateState(row["from_state"]) if row["from_state"] else None
                ),
                to_state=CandidateState(row["to_state"]),
                acted_at=datetime.fromisoformat(row["acted_at"]),
                detail=row["detail"],
            )
        )
    return grouped


# --- 统计与日期 ---


def count_processed(
    conn: sqlite3.Connection,
    candidate_ids: tuple[str, ...],
    states: tuple[CandidateState, ...],
) -> int:
    """这些候选项里已处于处理终态的只数；本轮统计按浏览步骤清点。"""
    if not candidate_ids:
        return 0
    placeholders = ",".join("?" for _ in candidate_ids)
    state_placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"""
        select count(*) from candidates
        where candidate_id in ({placeholders}) and state in ({state_placeholders})
        """,
        list(candidate_ids) + [s.value for s in states],
    ).fetchone()[0]


def list_dates(conn: sqlite3.Connection) -> list[str]:
    """全部入选日期，降序；跨日合并的候选项据此在任一天筛选中被找到。"""
    rows = conn.execute(
        "select distinct import_date from candidate_selections order by import_date desc"
    ).fetchall()
    return [r["import_date"] for r in rows]


def list_clearable(
    conn: sqlite3.Connection,
    states: tuple[CandidateState, ...],
    import_date: str | None,
) -> list[tuple[str, CandidateState]]:
    """列出当前可被主动清理的候选项及其原状态。

    返回 id 与状态而不是直接批量改状态：清理也要写进历次处理记录，
    因此由服务层逐项迁移状态并留痕，保持"来源与处理记录"可追溯。
    """
    sql = (
        "select candidate_id, state from candidates "
        f"where state in ({','.join('?' for _ in states)})"
    )
    params: list[object] = [s.value for s in states]
    if import_date:
        sql += (
            " and exists (select 1 from candidate_selections sel "
            "where sel.candidate_id = candidates.candidate_id and sel.import_date = ?)"
        )
        params.append(import_date)
    rows = conn.execute(sql, params).fetchall()
    return [(row["candidate_id"], CandidateState(row["state"])) for row in rows]


# --- 浏览上下文 ---


def get_state(conn: sqlite3.Connection) -> ClassificationState:
    row = conn.execute("select * from classification_state where id = 1").fetchone()
    if row is None:
        return ClassificationState()
    return ClassificationState(
        current_candidate_id=row["current_candidate_id"],
        view_mode=row["view_mode"],
        scope=CandidateScope(row["scope"]),
        import_date=row["import_date"],
        search=row["search"] or "",
        result=CandidateState(row["result"]) if row["result"] else None,
        round_started_at=(
            datetime.fromisoformat(row["round_started_at"])
            if row["round_started_at"]
            else None
        ),
        filter_scope=CandidateScope(row["filter_scope"]),
        path=tuple(json.loads(row["path_json"])),
        cursor=row["cursor"],
        # 旧库的结束卡标记：那时结束卡还不是路径里的一步，读回时归一为一步。
        # 列可能不存在（更旧的库），此时按「没有结束卡」处理。
        ended=bool(row["ended"]) if "ended" in row.keys() else False,
        navigation_revision=row["navigation_revision"],
        list_revision=row["list_revision"],
    )


def save_state(
    conn: sqlite3.Connection, state: ClassificationState, updated_at: str
) -> None:
    """保存浏览上下文；列表版本由 bump_list_revision 在同一事务里推进。

    这里只回写读到的那份 list_revision，不用它做自增，避免浏览位置变化
    把列表版本也推高、让前端误以为列表过期。
    """
    state.navigation_revision += 1
    conn.execute(
        """
        insert into classification_state
            (id, current_candidate_id, view_mode, scope, import_date, search, result,
             round_started_at, filter_scope, path_json, cursor, ended, navigation_revision,
             list_revision, updated_at)
        values (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            current_candidate_id = excluded.current_candidate_id,
            view_mode = excluded.view_mode,
            scope = excluded.scope,
            import_date = excluded.import_date,
            search = excluded.search,
            result = excluded.result,
            round_started_at = excluded.round_started_at,
            filter_scope = excluded.filter_scope,
            path_json = excluded.path_json,
            cursor = excluded.cursor,
            ended = excluded.ended,
            navigation_revision = excluded.navigation_revision,
            list_revision = excluded.list_revision,
            updated_at = excluded.updated_at
        """,
        (
            state.current_candidate_id,
            state.view_mode,
            state.scope.value,
            state.import_date,
            state.search,
            state.result.value if state.result else None,
            state.round_started_at.isoformat() if state.round_started_at else None,
            state.filter_scope.value,
            json.dumps(state.path, ensure_ascii=False),
            state.cursor,
            int(state.ended),
            state.navigation_revision,
            state.list_revision,
            updated_at,
        ),
    )
