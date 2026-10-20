import pandas as pd
import pytest

from cta_lab.portfolio_data import parse_panel_month


@pytest.mark.parametrize("date_column", ["日期", "交易日期"])
def test_panel_preserves_oi_zero_volume_marks_and_source_calendar(monkeypatch, date_column):
    frame = pd.DataFrame(
        {
            "合约": ["Contract", "rb2505", None, "cu2505", "zn2505"],
            date_column: ["Date", 20240102, 20240103, 20240102, 20240104],
            "开盘价": ["Open", 100, 101, 200, 300],
            "最高价": ["High", 102, 103, 202, 302],
            "最低价": ["Low", 99, 100, 199, 299],
            "收盘价": ["Close", 101, 102, 201, 301],
            "成交量": ["Volume", 1000, 0, 2000, 3000],
            "持仓量": ["OI", 1500, 1200, -1, 1600],
        }
    )
    monkeypatch.setattr(pd, "read_excel", lambda *a, **kw: frame.copy())
    rows, calendar, omitted = parse_panel_month(b"artificial workbook", {"rb", "cu"})
    assert len(rows) == 2
    assert rows[1]["volume"] == 0
    assert rows[1]["open_interest"] == 1200
    assert calendar == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert omitted == [
        {"datetime": "2024-01-02", "vt_symbol": "cu2505.SHFE", "reason": "invalid_OHLCV_or_OI"}
    ]


def test_unknown_date_format_cannot_silently_drop_an_entire_month(monkeypatch):
    frame = pd.DataFrame(
        {
            "合约": ["rb2505"],
            "日期": ["not-a-trading-date"],
            "开盘价": [100],
            "最高价": [101],
            "最低价": [99],
            "收盘价": [100],
            "成交量": [1000],
            "持仓量": [2000],
        }
    )
    monkeypatch.setattr(pd, "read_excel", lambda *a, **kw: frame.copy())
    with pytest.raises(ValueError, match="no recognized trading dates"):
        parse_panel_month(b"bad dates", {"rb"})
