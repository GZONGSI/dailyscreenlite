"""真实子进程 → 本地供应商替身：超时回收与数据序列化，不访问外网。"""
import os
from pathlib import Path
from time import monotonic

import pytest

from dailyscreen_lite.quotes import akshare_worker
from dailyscreen_lite.quotes.source import QuoteSourceError


def test_worker_kills_stalled_request_and_next_request_can_succeed(tmp_path, monkeypatch):
    stub = tmp_path / 'akshare.py'
    stub.write_text('import time\ndef stock_tfp_em(**kwargs):\n    time.sleep(30)\n', encoding='utf-8')
    source_root = Path(__file__).resolve().parents[1] / 'src'
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join((str(tmp_path), str(source_root))))
    monkeypatch.setattr(akshare_worker, 'REQUEST_TIMEOUT', 0.3)
    started = monotonic()
    with pytest.raises(QuoteSourceError, match='请求超过'):
        akshare_worker.fetch_frame('stock_tfp_em', date='20260918')
    assert monotonic() - started < 5
    stub.write_text("import pandas as pd\ndef stock_tfp_em(**kwargs):\n    print('SDK 日志')\n    return pd.DataFrame([{'代码': '000001', '成交额': None, '日期': '2026-09-18'}])\n", encoding='utf-8')
    monkeypatch.setattr(akshare_worker, 'REQUEST_TIMEOUT', 10)
    frame = akshare_worker.fetch_frame('stock_tfp_em', date='20260918')
    assert frame.iloc[0]['代码'] == '000001'
    assert frame.iloc[0]['成交额'] is None
    assert frame.iloc[0]['日期'] == '2026-09-18'
