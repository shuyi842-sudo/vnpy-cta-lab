"""Point-in-time contract panel from official SHFE archives, with OI and calendar audit."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

from .data import file_sha256
from .portfolio_model import product_of
from .shfe import INDEX

PANEL_FIELDS = [
    "datetime",
    "vt_symbol",
    "product",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
]


def parse_panel_month(raw: bytes, products: set[str]) -> tuple[list[dict], list[str], list[dict]]:
    frame = pd.read_excel(io.BytesIO(raw), header=2, dtype=object)
    frame.columns = [str(x).strip() for x in frame.columns]
    frame = frame.rename(columns={"交易日期": "日期"})
    names = {
        "开盘价": "open",
        "最高价": "high",
        "最低价": "low",
        "收盘价": "close",
        "成交量": "volume",
        "持仓量": "open_interest",
    }
    if not {"合约", "日期", *names}.issubset(frame.columns):
        raise ValueError(f"unexpected official panel columns: {list(frame.columns)}")
    dates = pd.to_datetime(
        frame["日期"].astype(str).str.strip().str.removesuffix(".0"),
        format="%Y%m%d",
        errors="coerce",
    )
    calendar = sorted(set(dates.dropna().dt.strftime("%Y-%m-%d")))
    if not calendar:
        raise ValueError("official workbook has no recognized trading dates")
    frame["合约"] = frame["合约"].ffill().astype(str).str.strip().str.lower()
    frame["product"] = frame["合约"].str.extract(r"^([a-z]+)\d{4}$", expand=False)
    frame["datetime"] = dates.dt.strftime("%Y-%m-%d")
    frame = frame.loc[frame["product"].isin(products) & dates.notna()].copy()
    frame["vt_symbol"] = frame["合约"] + ".SHFE"
    for source, target in names.items():
        frame[target] = pd.to_numeric(
            frame[source].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
    frame = frame[PANEL_FIELDS]
    numeric = frame[PANEL_FIELDS[3:]]
    finite = numeric.notna().all(axis=1) & numeric.abs().lt(float("inf")).all(axis=1)
    valid = finite & frame[["open", "high", "low", "close"]].gt(0).all(axis=1)
    valid &= frame[["volume", "open_interest"]].ge(0).all(axis=1)
    valid &= frame["low"].le(frame[["open", "close"]].min(axis=1)) & frame["high"].ge(
        frame[["open", "close"]].max(axis=1)
    )
    omissions = [
        {
            "datetime": row["datetime"],
            "vt_symbol": row["vt_symbol"],
            "reason": "invalid_OHLCV_or_OI",
        }
        for row in frame.loc[~valid].to_dict("records")
    ]
    return frame.loc[valid].to_dict("records"), calendar, omissions


def prepare_panel(
    years: list[int], cfg: dict, cache: Path, out: Path, freeze: Path | None = None
) -> dict:
    if any(year >= int(cfg["holdout"][0][:4]) for year in years):
        if freeze is None:
            raise ValueError(
                "holdout archive requires a verified parameter freeze before downloading"
            )
        from .portfolio_research import verify_freeze

        verify_freeze(freeze, cfg)
    cache.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    index_file = cache / "shfe_download.json"
    if index_file.exists():
        index = json.loads(index_file.read_text(encoding="utf-8"))
    else:
        response = requests.get(INDEX, timeout=30)
        response.raise_for_status()
        index = response.json()
        index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    records, calendar, omissions, sources = {}, set(), [], []
    for year in sorted(set(years)):
        entries = index["data"]["historicalData"] + index["data"]["latestData"]
        item = next(x for x in entries if x["year"] == str(year) and x["type"] == "1")
        url = urljoin("https://www.shfe.com.cn", item["url"])
        if urlparse(url).netloc != "www.shfe.com.cn":
            raise ValueError("unexpected data host")
        archive = cache / f"shfe_{year}.zip"
        if not archive.exists():
            print(f"Downloading official SHFE archive {year}", flush=True)
            response = requests.get(url, timeout=180)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                if zf.testzip():
                    raise ValueError("corrupt archive")
            temporary = archive.with_suffix(".part")
            temporary.write_bytes(response.content)
            temporary.replace(archive)
        archive_hash = file_sha256(archive)
        sources.append({"year": year, "url": url, "sha256": archive_hash})
        with zipfile.ZipFile(archive) as zf:
            members = sorted(
                n
                for n in zf.namelist()
                if re.search(r"20\d{2}\.\d{1,2}\.xlsx?$", n) and not n.startswith("ine")
            )
            if not members:
                raise ValueError("official archive has no SHFE monthly workbooks")
            for member in members:
                cache_key = hashlib.sha256(
                    (
                        "panel-v1" + archive_hash + member + ",".join(sorted(cfg["products"]))
                    ).encode()
                ).hexdigest()
                normalized = cache / "normalized-panels" / (cache_key + ".json")
                if normalized.exists():
                    rows, days, bad = json.loads(normalized.read_text(encoding="utf-8"))
                else:
                    print(f"Parsing {year}: {member.rsplit('/', 1)[-1]}", flush=True)
                    rows, days, bad = parse_panel_month(zf.read(member), set(cfg["products"]))
                    normalized.parent.mkdir(parents=True, exist_ok=True)
                    normalized.write_text(
                        json.dumps([rows, days, bad], ensure_ascii=False, allow_nan=False),
                        encoding="utf-8",
                    )
                calendar.update(days)
                omissions.extend(bad)
                for row in rows:
                    key = row["datetime"], row["vt_symbol"]
                    if key in records and records[key] != row:
                        raise ValueError(f"conflicting contract/date: {key}")
                    records[key] = row
    rows = [records[key] for key in sorted(records)]
    if not rows:
        raise ValueError("empty contract panel")
    coverage = {product: set() for product in cfg["products"]}
    for row in rows:
        coverage[row["product"]].add(row["datetime"])
    missing = {
        product: sorted(calendar - days) for product, days in coverage.items() if calendar - days
    }
    if missing:
        raise ValueError(f"product coverage missing source-observed exchange dates: {missing}")
    path = out / "panel.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PANEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "schema": "actual-contract-OHLCV-OI-v1",
        "source_index": INDEX,
        "sources": sources,
        "products": sorted(cfg["products"]),
        "calendar": sorted(calendar),
        "calendar_kind": "dates observed in all products of official source workbooks, not an independently licensed exchange holiday calendar",
        "rows": len(rows),
        "contracts": len({row["vt_symbol"] for row in rows}),
        "first_date": rows[0]["datetime"],
        "last_date": rows[-1]["datetime"],
        "csv_sha256": file_sha256(path),
        "omissions": omissions,
        "zero_volume_rows": sum(row["volume"] == 0 for row in rows),
    }
    if freeze is not None:
        manifest["selection_lock_sha256"] = file_sha256(freeze)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def load_panel(directory: Path, cfg: dict, maximum_date: str | None = None):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    path = directory / "panel.csv"
    if file_sha256(path) != manifest["csv_sha256"]:
        raise ValueError("contract panel hash differs from its source manifest")
    if maximum_date and manifest["last_date"] > maximum_date:
        raise ValueError("development loader refuses data beyond the selection boundary")
    frame = pd.read_csv(path)
    if (
        frame.columns.tolist() != PANEL_FIELDS
        or len(frame) != manifest["rows"]
        or frame.duplicated(["datetime", "vt_symbol"]).any()
    ):
        raise ValueError("invalid contract panel schema, count or duplicates")
    bars = []
    for row in frame.to_dict("records"):
        if row["product"] not in cfg["products"] or row["product"] != product_of(row["vt_symbol"]):
            raise ValueError("panel product mismatch")
        if maximum_date and row["datetime"] > maximum_date:
            raise ValueError("future row in development panel")
        bars.append(
            BarData(
                symbol=row["vt_symbol"].split(".")[0],
                exchange=Exchange.SHFE,
                datetime=datetime_from_date(row["datetime"]),
                interval=Interval.DAILY,
                open_price=row["open"],
                high_price=row["high"],
                low_price=row["low"],
                close_price=row["close"],
                volume=row["volume"],
                open_interest=row["open_interest"],
                gateway_name="SHFE_PANEL",
            )
        )
    return bars, manifest


def datetime_from_date(value):
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%d")
