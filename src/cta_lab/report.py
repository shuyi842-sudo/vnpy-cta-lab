"""Standalone offline report and machine-readable research evidence."""

from __future__ import annotations

import html
import importlib.metadata
import json
import platform
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .runner import Run


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )


def export_report(
    run: Run,
    out: Path,
    provenance: dict,
    sensitivity: list,
    oos_daily: pd.DataFrame | None = None,
    oos: dict | None = None,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    run.daily.drop(columns=["trades"], errors="ignore").to_csv(
        out / "daily.csv", encoding="utf-8-sig"
    )
    run.trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    write_json(out / "events.json", run.audit)
    evidence = {
        "provenance": provenance,
        "contract": run.contract,
        "settings": run.settings,
        "full_sample": run.statistics,
        "cost_sensitivity_full_sample": sensitivity,
        "walk_forward": oos,
        "runtime": {
            "python": platform.python_version(),
            **{
                name: importlib.metadata.version(name)
                for name in ("vnpy", "vnpy_ctastrategy", "numpy", "pandas", "plotly")
            },
        },
    }
    write_json(out / "evidence.json", evidence)
    if oos_daily is not None:
        oos_daily.drop(columns=["trades"], errors="ignore").to_csv(
            out / "oos_daily.csv", encoding="utf-8-sig"
        )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        subplot_titles=("Full-sample equity (not out-of-sample)", "Full-sample drawdown"),
    )
    capital = run.contract["capital"]
    equity = capital + run.daily.net_pnl.cumsum()
    fig.add_trace(go.Scatter(x=run.daily.index, y=equity, name="Full sample"), row=1, col=1)
    fig.add_trace(
        go.Scatter(
            x=run.daily.index,
            y=[capital] * len(equity),
            name="Flat cash (0% interest)",
            line={"dash": "dash"},
        ),
        row=1,
        col=1,
    )
    dd = (equity / equity.cummax().clip(lower=capital) - 1) * 100
    fig.add_trace(
        go.Scatter(x=run.daily.index, y=dd, name="Drawdown %", fill="tozeroy"), row=2, col=1
    )
    fig.update_layout(template="plotly_white", height=650, margin={"t": 60, "b": 40})
    chart = fig.to_html(full_html=False, include_plotlyjs=oos_daily is None)
    oos_chart = ""
    if oos_daily is not None:
        fig_oos = go.Figure(
            go.Scatter(
                x=oos_daily.index, y=capital + oos_daily.net_pnl.cumsum(), name="Walk-forward OOS"
            )
        )
        fig_oos.update_layout(
            title="Out-of-sample equity",
            template="plotly_white",
            height=350,
        )
        oos_chart = fig_oos.to_html(full_html=False, include_plotlyjs=True)
    kind = provenance["data_kind"]
    label = (
        "合成数据 · 仅验证工程流程，不代表市场收益"
        if kind == "synthetic"
        else "真实行情研究 · 数据来源、授权与合约规则见下方说明"
    )
    stat_table = pd.DataFrame([run.statistics]).T.to_html(
        header=False, escape=True, float_format=lambda x: f"{x:,.6f}"
    )
    oos_table = (
        pd.DataFrame([oos["statistics"]]).T.to_html(
            header=False, escape=True, float_format=lambda x: f"{x:,.6f}"
        )
        if oos
        else ""
    )
    source = html.escape(provenance["source"])
    body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CTA Lab · Research Evidence</title>
<style>*{{box-sizing:border-box}}body{{font:16px/1.65 system-ui,'Microsoft YaHei',sans-serif;margin:32px auto;padding:0 22px;max-width:1150px;color:#17243a;background:#f7f9fc;overflow-wrap:anywhere}}h1{{font-size:34px}}.notice{{padding:16px;border-left:5px solid #d18a00;background:#fff1cf}}table{{border-collapse:collapse;background:white}}td,th{{padding:8px 14px;border:1px solid #d5dce7;text-align:left}}.table-scroll{{max-width:100%;overflow-x:auto}}.table-scroll table{{font-size:14px}}code{{background:#e7edf5;padding:2px 5px}}section{{margin:32px 0;min-width:0}}.plotly-graph-div{{max-width:100%}}footer{{color:#5d6c7c}}@media(max-width:600px){{body{{padding:0 14px;margin:18px auto}}h1{{font-size:26px}}td,th{{padding:7px 10px}}}}</style>
<h1>CTA Lab / vn.py 研究报告</h1><p class="notice">{label}</p>
<p>来源：{source}<br>输入 SHA-256：<code>{provenance["sha256"]}</code></p>
<p>日线收盘产生信号，最早下一根 K 线成交；采用 vn.py 限价撮合与逐日盯市。最后两根 K 线用于退出，不新增风险。</p>
<section><h2>样本外验证</h2>{oos_chart}{oos_table}<p>{"训练参数、日期与各折结果见 evidence.json。" if oos else "本次未执行；不能声称已通过样本外验证。"}</p></section>
<section>{chart}</section><section><h2>全样本指标（仅描述，不代表样本外表现）</h2>{stat_table}</section>
<section><h2>成本敏感性（全样本固定参数）</h2>{pd.DataFrame(sensitivity).to_html(index=False, escape=True)}</section>
<section><h2>解释边界</h2><p>基准为不持仓且零利息的现金。手续费为成交额比例，滑点按每手每边固定价格扣款；不模拟成交量容量、保证金追缴、涨跌停队列及真实柜台。止损按收盘判断，跳空可能扩大亏损。仓位预算基于固定初始资金；不是资金复利再平衡。</p>
<p>单合约数据不自动换月、不复权；连续合约不得直接当可交易合约。无交易日历校验，不会把周末、节假日或缺失数据自行补齐。最终退出未成交会报错。</p></section>
<footer>可核查文件：daily.csv · trades.csv · events.json · evidence.json · oos_daily.csv（如有）。收益结果不构成实盘能力证明。</footer></html>"""
    body = body.replace("<table", '<div class="table-scroll"><table').replace(
        "</table>", "</table></div>"
    )
    (out / "report.html").write_text(body, encoding="utf-8")
