"""文本编码解码：UTF-8（含 BOM）优先，其次 GBK，严格失败。

能解码不等于能准确判断来源编码，因此不做猜测性替换；无法解码即明确报错。
"""

from __future__ import annotations

from dataclasses import dataclass

from dailyscreen_lite.domain.errors import DecodeFailed


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str


def decode_bytes(data: bytes) -> DecodedText:
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return DecodedText(data.decode("utf-8-sig"), "utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DecodeFailed("文件为 UTF-8 BOM 编码但内容无法解码", detail=str(exc)) from exc
    for encoding in ("utf-8", "gbk"):
        try:
            return DecodedText(data.decode(encoding), encoding)
        except UnicodeDecodeError:
            continue
    raise DecodeFailed("无法按 UTF-8 或 GBK 解码文件，请另存为受支持的编码后重试")
