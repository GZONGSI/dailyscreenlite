"""领域层：模型、时间与错误分类。不依赖 Web 框架和数据库。"""

from dailyscreen_lite.domain.clock import BeijingClock, Clock
from dailyscreen_lite.domain.errors import (
    ImportRejected,
    ParseFailed,
    UnsupportedFormat,
)
from dailyscreen_lite.domain.models import (
    BatchStatus,
    Candidate,
    CandidateScope,
    CandidateState,
    ImportBatch,
    ImportDate,
    ParsedCandidate,
    ParseResult,
    Security,
    SourceKind,
    StockOutcome,
)

__all__ = [
    "BeijingClock",
    "Clock",
    "ImportRejected",
    "ParseFailed",
    "UnsupportedFormat",
    "BatchStatus",
    "Candidate",
    "CandidateScope",
    "CandidateState",
    "ImportBatch",
    "ImportDate",
    "ParsedCandidate",
    "ParseResult",
    "Security",
    "SourceKind",
    "StockOutcome",
]
