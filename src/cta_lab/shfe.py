"""Download official annual archives; read raw workbooks without modifying them."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .data import FIELDS, read_bars

INDEX = "https://www.shfe.com.cn/reports/tradedata/datadownload/download.json"


def parse_month(raw: bytes) -> list[dict]:
    """SHFE Chinese header, English subheader, merged contract names and totals."""
    import pandas as pd

    frame = pd.read_excel(io.BytesIO(raw), header=2, dtype=object)
    frame.columns = [str(x).strip() for x in frame.columns]
    frame = frame.rename(columns={"交易日期": "日期"})
    required = ["合约", "日期", "开盘价", "最高价", "最低价", "收盘价", "成交量"]
    if not set(required).issubset(frame.columns):
        raise ValueError(f"unrecognized official workbook columns: {list(frame.columns)}")
    frame["合约"] = frame["合约"].ffill()
    rows = []
    for item in frame[required].to_dict("records"):
        symbol = str(item["合约"]).strip().lower()
        day = str(item["日期"]).strip().removesuffix(".0")
        if not re.fullmatch(r"[a-z]+\d{4}", symbol) or not re.fullmatch(r"\d{8}", day):
            continue  # Explicit English subheader and subtotal/footer exclusion.
        row = {"symbol": symbol, "datetime": datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d")}
        for target, source in zip(FIELDS[1:], required[2:], strict=True):
            try:
                row[target] = float(str(item[source]).replace(",", ""))
            except ValueError:
                row[target] = None
        rows.append(row)
    return rows


def convert_archives(paths: list[Path], symbols: list[str], out: Path, sources: list[dict]) -> dict:
    """One CSV per actual contract; never concatenate prices across maturities."""
    out.mkdir(parents=True, exist_ok=True)
    results = {symbol: {} for symbol in symbols}
    omitted = {symbol: [] for symbol in symbols}
    for archive in paths:
        with zipfile.ZipFile(archive) as zf:
            members = sorted(
                n
                for n in zf.namelist()
                if re.search(r"20\d{2}\.\d{1,2}\.xlsx?$", n) and not n.startswith("ine")
            )
            if not members:
                raise ValueError(f"no monthly SHFE workbook in {archive.name}")
            for name in members:
                print(f"Reading {archive.name}: {name.rsplit('/', 1)[-1]}", flush=True)
                for row in parse_month(zf.read(name)):
                    symbol = row.pop("symbol")
                    if symbol not in results:
                        continue
                    # Stop before delivery month. This is NOT a liquidity filter.
                    expiry_month = f"20{symbol[-4:-2]}-{symbol[-2:]}-01"
                    if row["datetime"] >= expiry_month:
                        omitted[symbol].append(
                            {"date": row["datetime"], "reason": "delivery_month"}
                        )
                        continue
                    prices = [row[x] for x in FIELDS[1:5]]
                    if (
                        any(x is None or not math.isfinite(x) or x <= 0 for x in prices)
                        or row["volume"] is None
                        or not math.isfinite(row["volume"])
                        or row["volume"] <= 0
                    ):
                        omitted[symbol].append(
                            {"date": row["datetime"], "reason": "no_valid_traded_OHLCV"}
                        )
                        continue
                    day = row["datetime"]
                    if day in results[symbol] and results[symbol][day] != row:
                        raise ValueError(f"conflicting duplicate {symbol} {day}")
                    results[symbol][day] = row
    manifest = {
        "provider": "Shanghai Futures Exchange",
        "index_url": INDEX,
        "archives": sources,
        "policy": "actual contracts, no roll or adjustment; exclude delivery month and invalid/no-volume bars; all omissions listed",
        "contracts": {},
    }
    for symbol, by_date in results.items():
        path = out / f"{symbol}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(by_date[day] for day in sorted(by_date))
        bars = read_bars(path, f"{symbol}.SHFE")
        manifest["contracts"][symbol] = {
            "rows": len(bars),
            "first_date": bars[0].datetime.isoformat(),
            "last_date": bars[-1].datetime.isoformat(),
            "omitted": omitted[symbol],
            "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read official SHFE annual archives; raw data stays local"
    )
    parser.add_argument("--years", nargs="+", type=int, default=[2024, 2025])
    parser.add_argument("--contracts", nargs="+", default=["rb2505", "rb2510"])
    parser.add_argument("--cache", type=Path, default=Path("data/downloads"))
    parser.add_argument("--out", type=Path, default=Path("data/private/shfe"))
    args = parser.parse_args(argv)
    if any(not re.fullmatch(r"[a-z]+\d{4}", symbol) for symbol in args.contracts):
        parser.error("contracts must be lowercase actual SHFE symbols, e.g. rb2505")
    args.cache.mkdir(parents=True, exist_ok=True)
    index_path = args.cache / "shfe_download.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        response = requests.get(INDEX, timeout=30)
        response.raise_for_status()
        index = response.json()
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    paths, sources = [], []
    for year in sorted(set(args.years)):
        item = next(
            x
            for x in index["data"]["historicalData"]
            if x["year"] == str(year) and x["type"] == "1"
        )
        url = urljoin("https://www.shfe.com.cn", item["url"])
        if urlparse(url).netloc != "www.shfe.com.cn":
            raise ValueError("unexpected archive host")
        path = args.cache / f"shfe_{year}.zip"
        if not path.exists():
            print(f"Downloading official archive {year}", flush=True)
            response = requests.get(url, timeout=120)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                if zf.testzip():
                    raise ValueError("corrupt ZIP")
            temporary = path.with_suffix(".part")
            temporary.write_bytes(response.content)
            temporary.replace(path)
        paths.append(path)
        sources.append(
            {"year": year, "url": url, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    manifest = convert_archives(paths, args.contracts, args.out, sources)
    print(
        json.dumps(
            {
                k: {x: v for x, v in item.items() if x != "omitted"}
                for k, item in manifest["contracts"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
