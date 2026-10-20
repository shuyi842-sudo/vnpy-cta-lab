"""Explicit offline CLI; initializes vn.py state in the current project directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auditable vn.py daily CTA lab")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser(
        "demo", help="synthetic engineering demonstration, not real-market research"
    )
    demo.add_argument("--out", type=Path, default=Path("artifacts/demo"))
    run = sub.add_parser("run", help="run one daily contract from CSV")
    run.add_argument("--csv", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--data-kind", choices=["real", "synthetic"], required=True)
    run.add_argument(
        "--source", required=True, help="provider, acquisition, rights and contract treatment"
    )
    run.add_argument("--walk-forward", action="store_true")
    run.add_argument("--train-bars", type=int, default=300)
    run.add_argument("--test-bars", type=int, default=100)
    run.add_argument("--out", type=Path, default=Path("artifacts/run"))
    args = parser.parse_args(argv)
    # Upstream otherwise initializes ~/.vntrader at import time.
    Path(".vntrader").mkdir(exist_ok=True)
    from .data import file_sha256, read_bars, write_fixture
    from .report import export_report
    from .research import cost_sensitivity, walk_forward
    from .runner import Contract, run_backtest

    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "demo":
        path = args.out / "synthetic_daily.csv"
        write_fixture(path)
        config = {"contract": {}, "strategy": {"entry_window": 40, "atr_multiple": 2.5}}
        kind, source, use_wf = (
            "synthetic",
            "Deterministic artificial OHLCV, seed=20261020; generated locally; no exchange data",
            True,
        )
    else:
        path = args.csv
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if set(config) != {"contract", "strategy"}:
            parser.error("config must contain exactly contract and strategy objects")
        kind, source, use_wf = args.data_kind, args.source, args.walk_forward
        if kind == "real" and config["contract"].get("vt_symbol", "DEMO.SHFE").startswith("DEMO."):
            parser.error("real data requires an explicit non-DEMO vt_symbol")
    contract = Contract(**config["contract"])
    bars = read_bars(path, contract.vt_symbol)
    result = run_backtest(bars, contract, config["strategy"])
    sensitivity = cost_sensitivity(bars, contract, config["strategy"])
    train, test = (300, 100) if args.command == "demo" else (args.train_bars, args.test_bars)
    oos_daily, oos = (
        walk_forward(bars, contract, train, test, config["strategy"]) if use_wf else (None, None)
    )
    provenance = {
        "data_kind": kind,
        "source": source,
        "sha256": file_sha256(path),
        "input_name": path.name,
        "bars": len(bars),
        "first_date": bars[0].datetime.isoformat(),
        "last_date": bars[-1].datetime.isoformat(),
        "schema": "single-contract daily trading dates; no continuous-contract adjustment",
    }
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        symbol = contract.vt_symbol.rsplit(".", 1)[0]
        if manifest["contracts"][symbol]["csv_sha256"] != provenance["sha256"]:
            raise ValueError("input CSV hash differs from source manifest")
        provenance["source_manifest"] = manifest
    export_report(result, args.out, provenance, sensitivity, oos_daily, oos)
    print(
        json.dumps(
            {
                "data_kind": kind,
                "statistics": result.statistics,
                "report": str((args.out / "report.html").resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
