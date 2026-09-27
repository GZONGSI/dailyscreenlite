"""观察组、成员关系与观察浏览上下文的持久化读写。

成员关系以 (group_id, security_id) 为主键，重复保存用 insert or ignore 保持幂等。
观察组只有删除、没有归档：删除组行会级联删除该组成员关系，但不动候选、笔记、
来源与处理历史，也不影响股票在其他组的关系。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from dailyscreen_lite.domain.models import (
    DEFAULT_OBSERVATION_GROUP_ID,
    DEFAULT_OBSERVATION_GROUP_NAME,
    DEFAULT_OBSERVATION_SORT,
    ObservationGroup,
    ObservationState,
    ObservedStock,
)


def _to_group(row: sqlite3.Row) -> ObservationGroup:
    return ObservationGroup(
        group_id=row["group_id"],
        name=row["name"],
        is_default=bool(row["is_default"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        member_count=row["member_count"] if "member_count" in row.keys() else 0,
    )


_GROUP_COLUMNS = """
    g.group_id, g.name, g.is_default, g.created_at,
    (select count(*) from observation_members m where m.group_id = g.group_id) as member_count
"""


# --- 种子标记 ---


def has_seed(conn: sqlite3.Connection, seed_key: str) -> bool:
    row = conn.execute(
        "select 1 from app_seeds where seed_key = ?", (seed_key,)
    ).fetchone()
    return row is not None


def mark_seed(conn: sqlite3.Connection, seed_key: str, created_at: str) -> None:
    conn.execute(
        "insert or ignore into app_seeds (seed_key, created_at) values (?, ?)",
        (seed_key, created_at),
    )


def ensure_default_group(
    conn: sqlite3.Connection, created_at: str, seed_key: str
) -> None:
    """首次启动种下默认组；已种过就不再创建，用户删掉后重启不会重建。

    种子标记与组是否存在分开：删除组后标记仍在，因此不会被一次重启补回来。
    """
    if has_seed(conn, seed_key):
        return
    mark_seed(conn, seed_key, created_at)
    conn.execute(
        """
        insert or ignore into observation_groups
            (group_id, name, is_default, created_at, updated_at)
        values (?, ?, 1, ?, ?)
        """,
        (
            DEFAULT_OBSERVATION_GROUP_ID,
            DEFAULT_OBSERVATION_GROUP_NAME,
            created_at,
            created_at,
        ),
    )


# --- 组管理 ---


def list_groups(conn: sqlite3.Connection) -> list[ObservationGroup]:
    """按默认组优先、其余按创建顺序返回全部现存组。"""
    rows = conn.execute(
        f"""
        select {_GROUP_COLUMNS}
        from observation_groups g
        order by g.is_default desc, g.created_at asc, g.group_id asc
        """
    ).fetchall()
    return [_to_group(r) for r in rows]


def get_group(conn: sqlite3.Connection, group_id: str) -> ObservationGroup | None:
    row = conn.execute(
        f"select {_GROUP_COLUMNS} from observation_groups g where g.group_id = ?",
        (group_id,),
    ).fetchone()
    return _to_group(row) if row else None


def find_by_name(conn: sqlite3.Connection, name: str) -> ObservationGroup | None:
    row = conn.execute(
        f"select {_GROUP_COLUMNS} from observation_groups g where g.name = ?",
        (name,),
    ).fetchone()
    return _to_group(row) if row else None


def insert_group(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    name: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        insert into observation_groups
            (group_id, name, is_default, created_at, updated_at)
        values (?, ?, 0, ?, ?)
        """,
        (group_id, name, created_at, created_at),
    )


def rename_group(conn: sqlite3.Connection, group_id: str, name: str, updated_at: str) -> None:
    conn.execute(
        "update observation_groups set name = ?, updated_at = ? where group_id = ?",
        (name, updated_at, group_id),
    )


def delete_group(conn: sqlite3.Connection, group_id: str) -> None:
    """删除组及其成员关系（成员行由外键级联删除）。

    候选处理状态、笔记、来源与处理历史都不在外键链上，因此完整保留。
    """
    conn.execute("delete from observation_groups where group_id = ?", (group_id,))


# --- 成员关系 ---


def memberships_for_security(conn: sqlite3.Connection, security_id: str) -> list[str]:
    rows = conn.execute(
        """
        select group_id from observation_members
        where security_id = ? order by group_id
        """,
        (security_id,),
    ).fetchall()
    return [r["group_id"] for r in rows]


def groups_by_security(
    conn: sqlite3.Connection, security_ids: list[str]
) -> dict[str, list[str]]:
    """批量读取各股票的现存组：观察列表与单组筛选共用它列出全部归属。"""
    if not security_ids:
        return {}
    placeholders = ",".join("?" for _ in security_ids)
    rows = conn.execute(
        f"""
        select security_id, group_id from observation_members
        where security_id in ({placeholders})
        order by security_id asc, group_id asc
        """,
        security_ids,
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["security_id"], []).append(row["group_id"])
    return grouped


def add_member(    conn: sqlite3.Connection, group_id: str, security_id: str, created_at: str
) -> None:
    """幂等新增成员：已有关系重复保存不产生第二行。"""
    conn.execute(
        """
        insert or ignore into observation_members (group_id, security_id, created_at)
        values (?, ?, ?)
        """,
        (group_id, security_id, created_at),
    )


def remove_members(
    conn: sqlite3.Connection, security_id: str, group_ids: list[str]
) -> int:
    """只删除指定的组成员关系；未选中的关系保留（组合归类动作的移出语义）。"""
    if not group_ids:
        return 0
    placeholders = ",".join("?" for _ in group_ids)
    return conn.execute(
        f"delete from observation_members where security_id = ? "
        f"and group_id in ({placeholders})",
        [security_id, *group_ids],
    ).rowcount


def remove_all_memberships(conn: sqlite3.Connection, security_id: str) -> int:
    """删除该股票的全部组关系（观察组页面的整组替换先清空再落选中组）。"""
    return conn.execute(
        "delete from observation_members where security_id = ?", (security_id,)
    ).rowcount


def observed_stocks(
    conn: sqlite3.Connection, group_id: str | None = None
) -> list[ObservedStock]:
    """观察列表：按证券去重，带全部现存组与加入时间。

    group_id 为空时是「全部观察股票」汇总视图：每只股票一行，
    加入时间取这些现存组中最近的一次；指定组时只保留该组成员，
    加入时间取组内那次加入，便于按组内加入顺序浏览。
    两种视图都列出该股票所属的全部现存组：切组只改变筛选，不隐藏其他归属。
    """
    if group_id is None:
        rows = conn.execute(
            """
            select m.security_id as security_id,
                   max(m.created_at) as joined_at
            from observation_members m
            group by m.security_id
            order by joined_at desc, m.security_id asc
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            select m.security_id as security_id, m.created_at as joined_at
            from observation_members m
            where m.group_id = ?
            order by m.created_at desc, m.security_id asc
            """,
            (group_id,),
        ).fetchall()
    memberships = groups_by_security(conn, [row["security_id"] for row in rows])
    return [
        ObservedStock(
            security_id=row["security_id"],
            group_ids=tuple(memberships.get(row["security_id"], [])),
            joined_at=datetime.fromisoformat(row["joined_at"]),
        )
        for row in rows
    ]


# --- 观察浏览上下文 ---


def get_state(conn: sqlite3.Connection) -> ObservationState:
    row = conn.execute(
        "select group_id, sort, current_security_id from observation_state where id = 1"
    ).fetchone()
    if row is None:
        return ObservationState()
    return ObservationState(
        group_id=row["group_id"],
        sort=row["sort"] or DEFAULT_OBSERVATION_SORT,
        current_security_id=row["current_security_id"],
    )


def save_state(conn: sqlite3.Connection, state: ObservationState, updated_at: str) -> None:
    conn.execute(
        """
        insert into observation_state (id, group_id, sort, current_security_id, updated_at)
        values (1, ?, ?, ?, ?)
        on conflict(id) do update set
            group_id = excluded.group_id,
            sort = excluded.sort,
            current_security_id = excluded.current_security_id,
            updated_at = excluded.updated_at
        """,
        (state.group_id, state.sort, state.current_security_id, updated_at),
    )
