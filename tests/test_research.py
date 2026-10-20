import pandas as pd

from cta_lab.data import read_bars, write_fixture
from cta_lab.research import walk_forward
from cta_lab.runner import Contract, run_backtest


def test_walk_forward_selection_does_not_see_test_prices(tmp_path):
    path = tmp_path / "bars.csv"
    write_fixture(path, count=230)
    bars = read_bars(path, "DEMO.SHFE")
    daily, evidence = walk_forward(bars, Contract(), train=120, test=40)
    changed = read_bars(path, "DEMO.SHFE")
    for bar in changed[120:]:
        for field in ("open_price", "high_price", "low_price", "close_price"):
            setattr(bar, field, getattr(bar, field) * 1.01)
    _, other = walk_forward(changed, Contract(), train=120, test=40)
    assert evidence["folds"][0]["training_scores"] == other["folds"][0]["training_scores"]
    assert evidence["folds"][0]["chosen"] == other["folds"][0]["chosen"]
    assert len(daily) == 80
    assert not daily.index.duplicated().any()
    assert evidence["unused_tail_bars"] == 30
    assert evidence["folds"][0]["train_end"] < evidence["folds"][0]["test_start"]


def test_drawdown_latch_prevents_new_entries(tmp_path):
    path = tmp_path / "bars.csv"
    write_fixture(path, count=200)
    bars = read_bars(path, "DEMO.SHFE")
    result = run_backtest(bars, Contract(), {"drawdown_limit": 0.000001})
    halt = next(event for event in result.audit if event["event"] == "risk" and event["halted"])
    assert all(
        event["target"] == 0
        for event in result.audit
        if event["event"] == "signal" and event["datetime"] >= halt["datetime"]
    )
    assert result.trades.iloc[-1]["datetime"] > halt["datetime"]


def test_identical_runs_are_reproducible(tmp_path):
    path = tmp_path / "bars.csv"
    write_fixture(path, count=200)
    bars = read_bars(path, "DEMO.SHFE")
    first = run_backtest(bars, Contract())
    second = run_backtest(bars, Contract())
    pd.testing.assert_frame_equal(first.trades, second.trades)
    assert first.statistics == second.statistics
