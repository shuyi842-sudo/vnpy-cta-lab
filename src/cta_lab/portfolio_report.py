"""Offline portfolio report; publish aggregates only, retain detailed local audit files."""

from __future__ import annotations

import html
import importlib.metadata
import platform
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .portfolio_research import write_json


def export_portfolio(result, cfg, out: Path, provenance: dict):
    out.mkdir(parents=True, exist_ok=True)
    result.daily.to_csv(out / "portfolio_daily.csv", encoding="utf-8-sig")
    result.snapshots.to_csv(out / "account.csv", encoding="utf-8-sig")
    result.contract_daily.to_csv(
        out / "contract_contributions.csv", index=False, encoding="utf-8-sig"
    )
    write_json(out / "events.json", result.events)
    fills = pd.DataFrame([e for e in result.events if e["event"] == "fill"])
    fills.to_csv(out / "fills.csv", index=False, encoding="utf-8-sig")
    if not fills.empty:
        fills.loc[fills.reason.str.startswith("roll_")].to_csv(
            out / "roll_legs.csv", index=False, encoding="utf-8-sig"
        )
    if result.contract_daily.empty:
        contribution = pd.DataFrame(
            0.0, index=list(cfg["products"]), columns=["net_pnl", "commission", "slippage", "fills"]
        )
    else:
        contribution = (
            result.contract_daily.groupby("product")[["net_pnl", "commission", "slippage", "fills"]]
            .sum()
            .reindex(list(cfg["products"]), fill_value=0)
        )
    public = {
        "provenance": provenance,
        "protocol": cfg,
        "statistics": result.statistics,
        "product_contributions": contribution.reset_index(names="product").to_dict("records"),
        "runtime": {
            "python": platform.python_version(),
            **{
                name: importlib.metadata.version(name)
                for name in ("vnpy", "vnpy_portfoliostrategy", "numpy", "pandas")
            },
        },
        "accounting": "single initial capital; cash-flow ledger reconciles to upstream portfolio daily PnL",
        "execution": "next-open marketable orders only; close-before-open; missing held bar fails; missing execution bar cancels order",
    }
    write_json(out / "evidence.json", public)
    if provenance["data_kind"] == "real":
        write_json(Path("docs/results/portfolio_holdout.summary.json"), public)
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        subplot_titles=(
            "Single-pool equity",
            "Gross notional / equity",
            "Estimated margin / equity",
        ),
    )
    account = result.snapshots
    for row, column, name in (
        (1, "equity", "Equity"),
        (2, "gross", "Gross exposure"),
        (3, "margin", "Estimated margin"),
    ):
        values = account[column] if row == 1 else account[column] / account.equity * 100
        fig.add_trace(go.Scatter(x=account.index, y=values, name=name), row=row, col=1)
    fig.update_layout(height=850, template="plotly_white", margin={"t": 60, "b": 30})
    fig.update_yaxes(ticksuffix="%", row=2, col=1)
    fig.update_yaxes(ticksuffix="%", row=3, col=1)
    stats = result.statistics
    table = pd.DataFrame(
        [
            ("初始共享权益（元）", f"{cfg['capital']:,.2f}"),
            ("期末共享权益（元）", f"{stats['end_equity']:,.2f}"),
            ("组合净收益率", f"{stats['total_return']:.3%}"),
            ("最大回撤", f"{stats['max_drawdown']:.3%}"),
            ("Sharpe", f"{stats['sharpe']:.4f}" if stats["sharpe"] is not None else "n.a."),
            ("成交笔数（每边）", stats["fills"]),
            ("换月相关成交成本（元）", f"{stats['roll_cost']:,.2f}"),
            ("换月选择次数", stats["roll_selections"]),
            ("拒单次数", stats["rejections"]),
        ],
        columns=["指标", "结果"],
    ).to_html(index=False, escape=True)
    product_table = contribution.rename(
        columns={
            "net_pnl": "净盈亏(元)",
            "commission": "手续费(元)",
            "slippage": "滑点(元)",
            "fills": "成交笔数",
        }
    ).to_html(escape=True, float_format=lambda x: f"{x:,.2f}")
    title = (
        "冻结参数后的独立测试"
        if provenance["data_kind"] == "real"
        else "合成工程演示：不能用于证明市场收益"
    )
    details = html.escape(str(provenance.get("period", provenance.get("source", ""))))
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTA Lab · Multi-product portfolio</title><style>*{{box-sizing:border-box}}body{{font:16px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:#f6f8fc;color:#17243a;max-width:1150px;margin:30px auto;padding:0 20px;overflow-wrap:anywhere}}h1{{font-size:32px}}.notice{{padding:16px;background:#fff1cf;border-left:5px solid #c68c00}}table{{border-collapse:collapse;background:white}}td,th{{padding:9px 14px;border:1px solid #d3dbe7;white-space:nowrap}}.scroll{{max-width:100%;overflow-x:auto}}section{{margin:28px 0;min-width:0}}@media(max-width:600px){{h1{{font-size:25px}}body{{padding:0 12px}}}}</style>
<h1>多品种 CTA / 因果换月与共享资金池</h1><p class="notice">{title}</p><p>{details}</p>
<p>固定品种：rb、cu、al。每个账户仅有一份初始资金。收盘选择合约与产生信号，下一日开盘检查共享权益、敞口、预计保证金和初始风险，再尝试成交。</p>
<section>{table}</section><section>{fig.to_html(full_html=False, include_plotlyjs=True)}</section>
<section><h2>全部固定品种的净盈亏贡献</h2><div class="scroll">{product_table}</div></section>
<section><h2>研究边界</h2><p>合约按已知持仓量、成交量和交割月份选择，只向后换月；旧仓未平不能开新仓。两腿分别收取手续费和滑点，不把合约价差当成收益。真实行情缺失时不使用昨日伪造 K 线成交。</p>
<p>统一 15% 保证金率为估算，非历史经纪商结算账单；权益按收盘价标记。涨跌停排队、成交容量、盘中保证金追缴未还原。三个品种包含相关的铜铝，九个月最终测试仍不能证明长期稳定超额收益。</p>
<p>验证评分与选择锁在研究目录中；最终测试入口不搜索参数。原始行情和逐笔价格保留在本地，公开文件只保存派生汇总与来源哈希。</p></section>
<footer>可复核：portfolio_daily.csv · account.csv · contract_contributions.csv · fills.csv · roll_legs.csv · events.json · evidence.json</footer></html>"""
    page = page.replace("<section><table", '<section><div class="scroll"><table').replace(
        "</table></section>", "</table></div></section>"
    )
    (out / "report.html").write_text(page, encoding="utf-8")
