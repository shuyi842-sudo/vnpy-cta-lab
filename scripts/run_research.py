"""One command: official source -> validation -> full sample and OOS -> reports."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--cache", type=Path, default=Path("data/downloads"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    Path(".vntrader").mkdir(exist_ok=True)
    if not args.skip_download:
        subprocess.run(
            [sys.executable, "-m", "cta_lab.shfe", "--cache", str(args.cache)], check=True
        )
    for symbol in ("rb2505", "rb2510"):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "cta_lab.cli",
                "run",
                "--csv",
                f"data/private/shfe/{symbol}.csv",
                "--config",
                f"config/{symbol}.json",
                "--data-kind",
                "real",
                "--source",
                "SHFE official 2024/2025 archives, actual contract, unadjusted, delivery month excluded; source manifest attached",
                "--walk-forward",
                "--train-bars",
                "120",
                "--test-bars",
                "40",
                "--out",
                f"artifacts/{symbol}",
            ],
            check=True,
        )
        evidence = json.loads(Path(f"artifacts/{symbol}/evidence.json").read_text(encoding="utf-8"))
        # Public evidence contains research aggregates and hashes, not source OHLCV.
        summary = {
            "symbol": symbol,
            "full_sample": evidence["full_sample"],
            "out_of_sample": evidence["walk_forward"]["statistics"],
            "cost_sensitivity_full_sample": evidence["cost_sensitivity_full_sample"],
            "runtime": evidence["runtime"],
            "contract": evidence["contract"],
            "input_sha256": evidence["provenance"]["sha256"],
            "archives": evidence["provenance"]["source_manifest"]["archives"],
            "unused_tail_bars": evidence["walk_forward"]["unused_tail_bars"],
            "folds": [
                {k: v for k, v in fold.items() if k not in {"trades", "events"}}
                for fold in evidence["walk_forward"]["folds"]
            ],
        }
        output = Path(f"docs/results/{symbol}.summary.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
