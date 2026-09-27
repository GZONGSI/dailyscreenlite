"""归类浏览路径的读写规则：步骤、游标与截断前进分支。

路径是本轮实际访问顺序的记录，每一步是候选卡或结束卡（`END_STEP`），允许同一
候选多次出现。这里只处理路径本身（不碰数据库），使「手动跳转截断前进分支」「沿
历史前后移动」「跳过失效步骤」这三条规则有单一实现，服务层只负责把结果落库。

约定：
- 游标总是指向当前步骤；路径为空时游标为 -1。
- 结束卡（`END_STEP`）是历史里的一步：前进与回看都停在它上面，两个方向都不跨过
  它。因此「A→B→结束卡→D」从 B 按「下一个」到结束卡、再按「下一个」才到 D，从 D
  按「上一个」回结束卡；结束卡之后的内容是一次跳转留下的前进历史，同样逐步访问。
- 候选是否存在由调用方判断（`exists`），因此本模块不依赖仓储。
"""

from __future__ import annotations

from collections.abc import Callable

from dailyscreen_lite.domain.models import END_STEP

CandidateExists = Callable[[str], bool]


def candidate_steps(path: tuple[str, ...]) -> tuple[str, ...]:
    """路径里出现过的候选项标识（去重、保持顺序）；结束卡不算候选。"""
    seen: dict[str, None] = {}
    for candidate_id in path:
        if candidate_id != END_STEP:
            seen.setdefault(candidate_id, None)
    return tuple(seen)


def truncate_after(path: tuple[str, ...], cursor: int) -> tuple[str, ...]:
    """手动跳转：丢掉当前步骤之后的前进分支，当前位置保持不变。"""
    if not path or cursor < 0:
        return ()
    return path[: cursor + 1]


def jump_to(
    path: tuple[str, ...], cursor: int, candidate_id: str
) -> tuple[tuple[str, ...], int]:
    """手动打开候选：截断前进分支后把目标作为新的一步追加，游标落在它上面。

    「再次访问同一候选形成新步骤」因此成立：从 C 退回 B 后打开 D，历史变成
    A→B→D，上一项回到本次跳转前的位置。只有目标就是当前这一步时保持原位置
    （界面点已选中的股票不该凭空多出一步）。

    从结束卡跳走时结束卡步骤保留：它是历史里可回看的一步，且保留它才使
    「结束卡拥有前进历史时下一个可用」成立。结束卡之后本来没有别的步骤，
    因此被替换掉的前进分支只是这次跳转自己。
    """
    if path and cursor >= 0 and path[cursor] == candidate_id:
        return path, cursor
    base = truncate_after(path, cursor)
    if base and base[-1] == END_STEP:
        steps = (*base, candidate_id)
        return steps, len(steps) - 1
    return (*base, candidate_id), len(base)


def locate(
    path: tuple[str, ...], candidate_id: str, cursor: int
) -> tuple[tuple[str, ...], int]:
    """就地打开某个候选：已是路径中的步骤就移动游标并保留前后历史。

    不存在时按一次新访问处理（截断前进分支后追加），使命令在任何情况下都能落到
    一张真实卡片上。
    """
    for index, step in enumerate(path):
        if step == candidate_id:
            return path, index
    return jump_to(path, cursor, candidate_id)


def append_end(path: tuple[str, ...], cursor: int) -> tuple[tuple[str, ...], int]:
    """在路径末端追加结束卡步骤，并把游标停在那里。

    已经停在结束卡上时不重复追加（同一次收尾只记一步）。结束卡之后仍有访问步骤时
    （从结束卡跳走留下的前进分支）**保留它们**，在路径末端再追加一步结束卡：收尾是
    一次导航，不删除已经访问过的历史，那些步骤仍然可以回看。因此路径里可能出现多个
    结束卡步骤，它们都是普通的一步。
    """
    if 0 <= cursor < len(path) and path[cursor] == END_STEP:
        return path, cursor
    steps = (*path, END_STEP)
    return steps, len(steps) - 1


def normalize(
    path: tuple[str, ...],
    cursor: int,
    *,
    exists: CandidateExists,
    ended: bool = False,
) -> tuple[tuple[str, ...], int, bool]:
    """读回时修补路径：丢掉已不存在的候选步骤，并把旧库的结束卡归一为一步。

    返回 (路径, 游标, 是否修补)。游标保持含义：指向与原来相同或最接近的有效步骤，
    因此失效步骤只是被跳过，不改变用户位置。真正不存在的候选只在这里被移除；
    主动清理过的候选仍在候选表里，因此保留为可回看的步骤（显示其最新状态）。
    结束卡步骤本身始终有效；它之后的步骤是一次跳转留下的前进历史，也要保留。

    `ended` 是旧库的结束卡标记（那时结束卡还不是路径里的一步，游标停在路径末端之后）：
    只在路径里还没有结束卡步骤时才用它补一步，位置与语义都不变，因此不需要数据迁移。
    """
    has_end_step = END_STEP in path
    if not path:
        # 旧库的空路径 + ended：结束卡当时靠「游标越过空路径」表达，补成一步。
        return ((END_STEP,), 0, True) if ended else ((), -1, False)
    valid = [
        index
        for index, step in enumerate(path)
        if step == END_STEP or exists(step)
    ]
    if not valid:
        steps = (END_STEP,) if ended else ()
        cursor = 0 if ended else -1
        return steps, cursor, True

    repaired = tuple(path[index] for index in valid)
    if cursor in valid:
        position = valid.index(cursor)
    else:
        # 当前步骤已不存在：落到它之前最近的有效步骤，没有就落到第一个。
        earlier = [index for index in valid if index < cursor]
        position = valid.index(earlier[-1]) if earlier else 0
    if ended and not has_end_step:
        repaired = (*repaired, END_STEP)
        position = len(repaired) - 1
    changed = repaired != path or position != cursor
    return repaired, position, changed


def next_position(
    path: tuple[str, ...],
    cursor: int,
    *,
    exists: CandidateExists,
    current_candidate_id: str | None,
) -> int | None:
    """沿已有路径前进：后一个步骤的位置；没有则 None。

    结束卡是历史的正常一步，前进时停在它上面（与 `previous_position` 对称），因此
    「A→B→结束卡→D」从 B 按「下一个」到结束卡、再按一次才到 D。到路径末端仍没有
    步骤时返回 None，由服务层去寻找尚未查看的新候选或追加结束卡。
    """
    return _position(path, cursor, 1, exists, current_candidate_id, allow_end=True)


def previous_position(
    path: tuple[str, ...],
    cursor: int,
    *,
    exists: CandidateExists,
    current_candidate_id: str | None,
) -> int | None:
    """回看：前一个有效步骤的位置；没有则 None。

    结束卡是历史的正常一步，回看时停在它上面（与 `next_position` 对称），因此从
    结束卡之后离开还能回来看它。
    """
    return _position(path, cursor, -1, exists, current_candidate_id, allow_end=True)


def _position(
    path: tuple[str, ...],
    cursor: int,
    step: int,
    exists: CandidateExists,
    current_candidate_id: str | None,
    *,
    allow_end: bool,
) -> int | None:
    start = cursor + step if cursor >= 0 else (len(path) - 1 if step < 0 else 0)
    for index in range(start, len(path) if step > 0 else -1, step):
        if not allow_end and path[index] == END_STEP:
            continue
        if _is_valid(path[index], exists, current_candidate_id):
            return index
    return None


def _is_valid(step: str, exists: CandidateExists, current_candidate_id: str | None) -> bool:
    return step == END_STEP or current_candidate_id == step or exists(step)
