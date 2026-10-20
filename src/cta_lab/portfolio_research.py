"""Development/selection first; a hash-locked, separately invoked final holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def code_hashes() -> dict:
    directory = Path(__file__).parent
    names = [
        "portfolio.py",
        "portfolio_model.py",
        "portfolio_data.py",
        "portfolio_research.py",
        "metrics.py",
        "data.py",
        "shfe.py",
    ]
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def validate_periods(cfg):
    windows = [
        tuple(datetime.fromisoformat(date) for date in cfg[name])
        for name in ("development", "validation", "holdout")
    ]
    if (
        any(start > end for start, end in windows)
        or windows[0][1] >= windows[1][0]
        or windows[1][1] >= windows[2][0]
    ):
        raise ValueError("development, validation and holdout must be ordered and non-overlapping")


def choose_candidate(rows: list[dict], minimum_fills: int) -> int:
    scores = []
    for row in rows:
        dev, val = row["development"], row["validation"]
        eligible = all(stats["solvent"] and stats["fills"] >= minimum_fills for stats in (dev, val))
        scores.append(val["sharpe"] if eligible and val["sharpe"] is not None else float("-inf"))
    return max(range(len(rows)), key=scores.__getitem__) if max(scores) != float("-inf") else 0


def verify_freeze(path: Path, cfg: dict) -> dict:
    validate_periods(cfg)
    if not path.exists():
        raise ValueError("parameter freeze missing; final-test data remains sealed")
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock["protocol_sha256"] != digest(cfg):
        raise ValueError("protocol changed after parameter freeze")
    if lock["code_hashes"] != code_hashes():
        raise ValueError(
            "research code changed after parameter freeze; cannot silently reuse holdout"
        )
    rows = lock["candidates"]
    expected = [
        {"entry_window": entry, "atr_multiple": atr}
        for entry in cfg["entry_windows"]
        for atr in cfg["atr_multiples"]
    ]
    if [row["setting"] for row in rows] != expected:
        raise ValueError("candidate grid differs from the frozen protocol")
    chosen = choose_candidate(rows, cfg["min_selection_fills"])
    if lock["chosen"] != rows[chosen]["setting"] or lock["chosen_index"] != chosen:
        raise ValueError("selected setting does not follow the fixed selection rule")
    if lock["development_last_date"] > cfg["validation"][1]:
        raise ValueError("selection evidence contains future dates")
    return lock


def select_parameters(panel: Path, cfg: dict, output: Path, cache: Path) -> dict:
    from .portfolio import run_portfolio
    from .portfolio_data import load_panel

    validate_periods(cfg)
    if output.exists():
        raise ValueError("selection lock already exists; refuse silent parameter reselection")
    if (cache / f"shfe_{cfg['holdout'][0][:4]}.zip").exists():
        raise ValueError(
            "holdout archive is already in this workspace cache; cannot claim first-use sealing"
        )
    root = Path(__file__).resolve().parents[2]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, check=True
    )
    if status.stdout.strip():
        raise ValueError("commit research implementation before selecting parameters")
    bars, manifest = load_panel(panel, cfg, maximum_date=cfg["validation"][1])
    candidates = []
    for entry in cfg["entry_windows"]:
        for atr in cfg["atr_multiples"]:
            setting = {"entry_window": entry, "atr_multiple": atr}
            development = run_portfolio(bars, cfg, setting, *cfg["development"])
            validation = run_portfolio(bars, cfg, setting, *cfg["validation"])
            candidates.append(
                {
                    "setting": setting,
                    "development": development.statistics,
                    "validation": validation.statistics,
                }
            )
            print("selection_candidate", json.dumps(candidates[-1], ensure_ascii=False), flush=True)
    index = choose_candidate(candidates, cfg["min_selection_fills"])
    lock = {
        "schema": "portfolio-selection-lock-v1",
        "created_utc": datetime.now(UTC).isoformat(),
        "source_git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "protocol_sha256": digest(cfg),
        "code_hashes": code_hashes(),
        "development_panel_sha256": manifest["csv_sha256"],
        "development_last_date": manifest["last_date"],
        "sources": manifest["sources"],
        "holdout_archive_absent_at_selection": True,
        "candidates": candidates,
        "chosen_index": index,
        "chosen": candidates[index]["setting"],
        "selection_rule": "maximum validation Sharpe after fixed development/validation fill+solvency eligibility; fixed grid-order tie-break; first-grid fallback",
    }
    write_json(output, lock)
    verify_freeze(output, cfg)
    return lock


def evaluate_holdout(development_panel: Path, test_panel: Path, cfg: dict, freeze: Path, out: Path):
    from .data import file_sha256
    from .portfolio import run_portfolio
    from .portfolio_data import load_panel
    from .portfolio_report import export_portfolio

    # Validate the lock before any final-test CSV is loaded or any search is run.
    lock = verify_freeze(freeze, cfg)
    development, development_manifest = load_panel(development_panel, cfg, cfg["validation"][1])
    if development_manifest["csv_sha256"] != lock["development_panel_sha256"]:
        raise ValueError("development data changed after selection")
    test_manifest = json.loads((test_panel / "manifest.json").read_text(encoding="utf-8"))
    if test_manifest.get("selection_lock_sha256") != file_sha256(freeze):
        raise ValueError("test data was not prepared under this exact selection lock")
    receipt_path = out / "holdout_receipt.json"
    identity = {
        "selection_lock_sha256": file_sha256(freeze),
        "test_panel_sha256": test_manifest["csv_sha256"],
        "code_hashes": code_hashes(),
    }
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        if previous["identity"] != identity:
            raise ValueError(
                "holdout was already consumed under different inputs; no silent re-evaluation"
            )
        first_access = previous["first_evaluation_utc"]
    else:
        first_access = datetime.now(UTC).isoformat()
    receipt = {"identity": identity, "first_evaluation_utc": first_access, "status": "started"}
    write_json(receipt_path, receipt)
    final_test, _ = load_panel(test_panel, cfg)
    if any(b.datetime.date().isoformat() < cfg["holdout"][0] for b in final_test):
        raise ValueError("test panel contains dates before the final-test boundary")
    result = run_portfolio(development + final_test, cfg, lock["chosen"], *cfg["holdout"])
    receipt.update(
        {"status": "completed", "statistics": result.statistics, "chosen": lock["chosen"]}
    )
    write_json(receipt_path, receipt)
    export_portfolio(
        result,
        cfg,
        out,
        {
            "data_kind": "real",
            "selection_lock_sha256": file_sha256(freeze),
            "source_git_commit": lock["source_git_commit"],
            "chosen": lock["chosen"],
            "development_sources": development_manifest["sources"],
            "test_sources": test_manifest["sources"],
            "period": cfg["holdout"],
            "receipt": receipt,
        },
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Causal rolling multi-product research with one equity pool"
    )
    parser.add_argument("--protocol", type=Path, default=Path("config/portfolio_protocol.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--phase", choices=["development", "holdout"], required=True)
    prepare.add_argument("--cache", type=Path, default=Path("data/downloads"))
    prepare.add_argument("--freeze", type=Path, default=Path("research/portfolio_selection.json"))
    select = sub.add_parser("select")
    select.add_argument("--panel", type=Path, default=Path("data/private/portfolio/development"))
    select.add_argument("--cache", type=Path, default=Path("data/downloads"))
    select.add_argument("--out", type=Path, default=Path("research/portfolio_selection.json"))
    test = sub.add_parser("holdout")
    test.add_argument(
        "--development", type=Path, default=Path("data/private/portfolio/development")
    )
    test.add_argument("--test-panel", type=Path, default=Path("data/private/portfolio/holdout"))
    test.add_argument("--freeze", type=Path, default=Path("research/portfolio_selection.json"))
    test.add_argument("--out", type=Path, default=Path("artifacts/portfolio/holdout"))
    demo = sub.add_parser("demo")
    demo.add_argument("--out", type=Path, default=Path("artifacts/portfolio/demo"))
    args = parser.parse_args(argv)
    Path(".vntrader").mkdir(exist_ok=True)
    cfg = json.loads(args.protocol.read_text(encoding="utf-8-sig"))
    validate_periods(cfg)
    if args.command == "prepare":
        from .portfolio_data import prepare_panel

        years = (
            list(range(int(cfg["development"][0][:4]), int(cfg["validation"][1][:4]) + 1))
            if args.phase == "development"
            else list(range(int(cfg["holdout"][0][:4]), int(cfg["holdout"][1][:4]) + 1))
        )
        manifest = prepare_panel(
            years,
            cfg,
            args.cache,
            Path("data/private/portfolio") / args.phase,
            args.freeze if args.phase == "holdout" else None,
        )
        print(
            json.dumps(
                {
                    key: manifest[key]
                    for key in ("rows", "contracts", "first_date", "last_date", "csv_sha256")
                },
                indent=2,
            )
        )
    elif args.command == "select":
        print(
            json.dumps(select_parameters(args.panel, cfg, args.out, args.cache)["chosen"], indent=2)
        )
    elif args.command == "holdout":
        print(
            json.dumps(
                evaluate_holdout(
                    args.development, args.test_panel, cfg, args.freeze, args.out
                ).statistics,
                indent=2,
            )
        )
    else:
        from .portfolio import run_portfolio
        from .portfolio_report import export_portfolio
        from .portfolio_synthetic import synthetic_panel

        bars = synthetic_panel(cfg)
        result = run_portfolio(
            bars, cfg, {"entry_window": 40, "atr_multiple": 2}, "2023-05-01", "2024-12-31"
        )
        export_portfolio(
            result,
            cfg,
            args.out,
            {
                "data_kind": "synthetic",
                "source": "locally generated artificial actual-maturity panel; no market-performance claim",
            },
        )
        print(json.dumps(result.statistics, indent=2))


if __name__ == "__main__":
    main()
