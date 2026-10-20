"""Replay the published frozen study with exact archive URLs and hashes, without search."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("data/downloads/portfolio-frozen"))
    parser.add_argument(
        "--reuse-archives", type=Path, help="optional local archive cache, verified before reuse"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    Path(".vntrader").mkdir(exist_ok=True)
    from cta_lab.portfolio_research import verify_freeze

    cfg = json.loads(Path("config/portfolio_protocol.json").read_text(encoding="utf-8"))
    freeze = Path("research/portfolio_selection.json")
    lock = verify_freeze(freeze, cfg)
    published = Path("docs/results/portfolio_holdout.summary.json")
    original_bytes = published.read_bytes()
    expected = json.loads(original_bytes)
    sources = lock["sources"] + expected["provenance"]["test_sources"]
    args.cache.mkdir(parents=True, exist_ok=True)
    entries = []
    for source in sources:
        if urlparse(source["url"]).netloc != "www.shfe.com.cn" or not source["url"].startswith(
            "https://"
        ):
            raise ValueError("unexpected archive origin")
        entries.append({"year": str(source["year"]), "type": "1", "url": source["url"]})
        target = args.cache / f"shfe_{source['year']}.zip"
        reuse = args.reuse_archives / target.name if args.reuse_archives else None
        if not target.exists() and reuse is not None and reuse.exists():
            if hashlib.sha256(reuse.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("existing archive differs from published study")
            # Same-volume link, with a copy fallback, never alters source archives.
            try:
                os.link(reuse.resolve(), target.resolve())
            except OSError:
                import shutil

                shutil.copyfile(reuse, target)
    (args.cache / "shfe_download.json").write_text(
        json.dumps({"data": {"historicalData": entries, "latestData": []}}), encoding="utf-8"
    )
    for phase in ("development", "holdout"):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "cta_lab.portfolio_research",
                "prepare",
                "--phase",
                phase,
                "--cache",
                str(args.cache),
            ],
            check=True,
        )
        for source in sources:
            if (source["year"] < 2026) != (phase == "development"):
                continue
            raw = args.cache / f"shfe_{source['year']}.zip"
            if hashlib.sha256(raw.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("official archive no longer matches the published study")
    manifest = json.loads(
        Path("data/private/portfolio/holdout/manifest.json").read_text(encoding="utf-8")
    )
    if manifest["csv_sha256"] != expected["provenance"]["receipt"]["identity"]["test_panel_sha256"]:
        raise ValueError("test panel differs from the published source hash")
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "cta_lab.portfolio_research",
                "holdout",
                "--out",
                "artifacts/portfolio/reproduction",
            ],
            check=True,
        )
        actual = json.loads(
            Path("artifacts/portfolio/reproduction/evidence.json").read_text(encoding="utf-8")
        )
    finally:
        # Preserve the original published first-evaluation receipt and statistics.
        published.write_bytes(original_bytes)
    for key, expected_value in expected["statistics"].items():
        actual_value = actual["statistics"][key]
        if isinstance(expected_value, (int, float)) and not isinstance(expected_value, bool):
            if not math.isclose(expected_value, actual_value, rel_tol=1e-10, abs_tol=1e-6):
                raise RuntimeError(f"published statistic differs: {key}")
        elif expected_value != actual_value:
            raise RuntimeError(f"published statistic differs: {key}")
    print(
        "Verified reproduction of all published portfolio statistics; no parameter search executed."
    )


if __name__ == "__main__":
    main()
