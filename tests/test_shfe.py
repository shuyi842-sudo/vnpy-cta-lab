import pandas as pd
import pytest

from cta_lab.shfe import parse_month


@pytest.mark.parametrize("date_column", ["日期", "交易日期"])
def test_official_header_versions_and_merged_contracts(monkeypatch, date_column):
    # Artificial rows model the official format; no third-party raw prices are vendored.
    frame = pd.DataFrame(
        {
            "合约": ["Contract", "rb2505", None, "小计"],
            date_column: ["Date", 20240102, 20240103, None],
            "开盘价": ["Open", 100, 101, None],
            "最高价": ["High", 102, 103, None],
            "最低价": ["Low", 99, 100, None],
            "收盘价": ["Close", 101, 102, None],
            "成交量": ["Volume", 200, 300, None],
        }
    )
    monkeypatch.setattr(pd, "read_excel", lambda *args, **kwargs: frame.copy())
    rows = parse_month(b"read-only format fixture")
    assert len(rows) == 2
    assert [x["symbol"] for x in rows] == ["rb2505", "rb2505"]
    assert [x["datetime"] for x in rows] == ["2024-01-02", "2024-01-03"]
    assert rows[1]["close"] == 102
    assert rows[1]["volume"] == 300
