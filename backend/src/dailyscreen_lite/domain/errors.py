"""导入流程的错误分类。

不同失败原因必须可区分，不能一概显示成"零结果"：
- 空来源（合法零结果）与全部未识别是两件事；
- 解析失败要与零结果分开，且不发布任何股票；
- 问财的鉴权异常、分页不完整与未知响应各自成类，不能解读为零结果。
"""

from __future__ import annotations

from dailyscreen_lite.domain.models import (
    SelectionOption,
    SelectionPreview,
    SelectionRequest,
)


class ImportRejected(Exception):
    """批次被拒绝，不得发布任何候选项。"""

    code = "import_rejected"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class UnsupportedFormat(ImportRejected):
    """来源格式不在支持范围内。路由层据此返回 400，不进入发布流程。"""

    code = "unsupported_format"


class ParseFailed(ImportRejected):
    code = "parse_failed"


class DecodeFailed(ParseFailed):
    code = "decode_failed"


class AmbiguousSelection(ImportRejected):
    """输入结构存在歧义，需要用户选择后再继续（不是失败，批次进入待选择）。

    携带候选项与最小预览；服务层据此把批次保留为待选择，而不是判定失败。
    """

    code = "ambiguous_selection"

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        options: list[SelectionOption],
        preview: SelectionPreview | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.kind = kind
        self.options = tuple(options)
        self.preview = preview or SelectionPreview()

    @property
    def request(self) -> SelectionRequest:
        return SelectionRequest(
            kind=self.kind,
            prompt=self.message,
            options=self.options,
            preview=self.preview,
        )


class AmbiguousColumns(AmbiguousSelection):
    """代码列无法唯一确定，需要用户选择。"""

    code = "ambiguous_columns"


class AmbiguousSheets(AmbiguousSelection):
    """工作簿含多个工作表，需要用户选择。"""

    code = "ambiguous_sheets"


class UnsupportedLink(ImportRejected):
    """链接不是已确认支持的问财桌面端结果链接。"""

    code = "unsupported_link"


class WencaiCookieMissing(ImportRejected):
    """未配置问财 Cookie，无法发起获取。"""

    code = "wencai_cookie_missing"


class WencaiAuthError(ImportRejected):
    """鉴权或访问异常（401/403）。不解读为零结果。"""

    code = "wencai_auth_error"


class WencaiLoginRequired(ImportRejected):
    """响应明确要求登录（登录失效），保留批次以便更新 Cookie 后重试。"""

    code = "wencai_login_required"


class WencaiTokenUnavailable(ImportRejected):
    """缺少可用的 hexin-v 令牌生成器，无法发起获取。"""

    code = "wencai_token_unavailable"


class WencaiIncomplete(ImportRejected):
    """分页遗漏、重复页、提前空页或数量不符，不能当作完整批次发布。"""

    code = "wencai_incomplete"


class WencaiUnexpected(ImportRejected):
    """响应结构未知或无法解析，不能当作零结果。"""

    code = "wencai_unexpected"
