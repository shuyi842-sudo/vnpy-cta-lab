import json
from copy import deepcopy
from pathlib import Path

import pytest

from cta_lab.portfolio_data import load_panel, prepare_panel
from cta_lab.portfolio_research import (
    choose_candidate,
    code_hashes,
    digest,
    evaluate_holdout,
    verify_freeze,
)


def protocol():
    return json.loads((Path(__file__).parents[1] / "config/portfolio_protocol.json").read_text())


def lock_fixture(tmp_path):
    cfg = protocol()
    candidates = []
    for i, setting in enumerate(
        {"entry_window": n, "atr_multiple": a}
        for n in cfg["entry_windows"]
        for a in cfg["atr_multiples"]
    ):
        candidates.append(
            {
                "setting": setting,
                "development": {"solvent": True, "fills": 20, "sharpe": 100 - i},
                "validation": {"solvent": True, "fills": 20, "sharpe": i + 0.1},
            }
        )
    lock = {
        "protocol_sha256": digest(cfg),
        "code_hashes": code_hashes(),
        "development_last_date": "2025-12-31",
        "candidates": candidates,
        "chosen_index": 5,
        "chosen": candidates[5]["setting"],
    }
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(lock))
    return cfg, path, lock


def test_selection_uses_validation_not_training_winner(tmp_path):
    cfg, path, lock = lock_fixture(tmp_path)
    assert choose_candidate(lock["candidates"], 12) == 5
    assert verify_freeze(path, cfg)["chosen"] == {"entry_window": 60, "atr_multiple": 3}
    rows = deepcopy(lock["candidates"])
    for row in rows:
        row["validation"]["fills"] = 0
    assert choose_candidate(rows, 12) == 0


def test_final_archive_cannot_be_downloaded_without_freeze(tmp_path, monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("network must not be touched while final test is sealed")

    monkeypatch.setattr(requests, "get", forbidden)
    with pytest.raises(ValueError, match="requires a verified parameter freeze"):
        prepare_panel([2026], protocol(), tmp_path / "cache", tmp_path / "out")


def test_changed_protocol_rejected_before_test_data_access(tmp_path):
    cfg, path, _ = lock_fixture(tmp_path)
    cfg["capital"] *= 2
    with pytest.raises(ValueError, match="protocol changed"):
        evaluate_holdout(
            tmp_path / "missing_dev", tmp_path / "missing_test", cfg, path, tmp_path / "out"
        )


def test_overlapping_selection_and_holdout_dates_are_rejected(tmp_path):
    cfg, path, _ = lock_fixture(tmp_path)
    cfg["holdout"][0] = "2025-12-31"
    with pytest.raises(ValueError, match="non-overlapping"):
        evaluate_holdout(
            tmp_path / "missing_dev", tmp_path / "missing_test", cfg, path, tmp_path / "out"
        )


def test_changed_research_code_rejected_before_test_data_access(tmp_path, monkeypatch):
    import cta_lab.portfolio_research as module

    cfg, path, _ = lock_fixture(tmp_path)
    monkeypatch.setattr(module, "code_hashes", lambda: {"modified": "different"})
    with pytest.raises(ValueError, match="code changed"):
        evaluate_holdout(
            tmp_path / "missing_dev", tmp_path / "missing_test", cfg, path, tmp_path / "out"
        )


def test_changing_chosen_setting_without_selection_evidence_is_rejected(tmp_path):
    cfg, path, lock = lock_fixture(tmp_path)
    lock["chosen_index"] = 0
    lock["chosen"] = lock["candidates"][0]["setting"]
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="does not follow"):
        verify_freeze(path, cfg)


def test_development_loader_refuses_future_panel_before_parsing_prices(tmp_path, monkeypatch):
    import hashlib

    import pandas as pd

    raw = b"must never parse this final-test CSV in parameter selection"
    (tmp_path / "panel.csv").write_bytes(raw)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"csv_sha256": hashlib.sha256(raw).hexdigest(), "last_date": "2026-01-02"})
    )
    monkeypatch.setattr(pd, "read_csv", lambda *a, **kw: pytest.fail("future prices were parsed"))
    with pytest.raises(ValueError, match="refuses data beyond"):
        load_panel(tmp_path, protocol(), "2025-12-31")
