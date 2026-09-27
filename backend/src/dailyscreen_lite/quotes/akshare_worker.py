"""AKShare 请求在独立短命进程运行，避免无超时的 SDK 阻塞全部恢复。"""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from functools import partial
from types import SimpleNamespace

REQUEST_TIMEOUT = 45
_METHODS = (
    "stock_info_sh_name_code", "stock_info_sz_name_code", "stock_info_bj_name_code",
    "stock_info_sh_delist", "stock_info_sz_delist", "stock_zh_a_hist",
    "stock_zh_a_hist_tx", "stock_zh_a_daily",
    "stock_tfp_em", "tool_trade_date_hist_sina",
)


def fetch_frame(method: str, **kwargs):
    from .source import QuoteSourceError
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "dailyscreen_lite.quotes.akshare_worker"],
            input=json.dumps({"method": method, "kwargs": kwargs}),
            capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise QuoteSourceError(f"AKShare {method} 失败：{result.stderr[-500:]}")
        import pandas as pd
        payload = json.loads(result.stdout)
        return pd.DataFrame(payload['rows'], columns=payload['columns'])
    except subprocess.TimeoutExpired as exc:
        raise QuoteSourceError(f"AKShare {method} 请求超过 {REQUEST_TIMEOUT} 秒") from exc
    except (OSError, ValueError, KeyError) as exc:
        raise QuoteSourceError(f"AKShare {method} 响应不可用：{exc}") from exc


def bounded_akshare():
    return SimpleNamespace(**{name: partial(fetch_frame, name) for name in _METHODS})


def main():
    request = json.load(sys.stdin)
    if request['method'] not in _METHODS:
        raise ValueError('不支持的 AKShare 请求')
    with contextlib.redirect_stdout(sys.stderr):
        import akshare as ak
        frame = getattr(ak, request['method'])(**request['kwargs'])
    # 日期序列化为 ISO 字符串，缺值为 null；保留空表列名供适配器正常判定。
    payload = json.loads(frame.to_json(orient='split', date_format='iso', force_ascii=False, double_precision=15))
    print(json.dumps({'columns': payload['columns'], 'rows': payload['data']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
