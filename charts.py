"""Plotly 图表。移动端关闭多余工具栏，关键数字同时以文字/表格展示。"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

UP_COLOR = "#d94841"      # A 股习惯：红涨
DOWN_COLOR = "#1f9d72"    # 绿跌
GRID = "rgba(148, 163, 184, 0.18)"
TEXT = "#e5e7eb"


def _base_layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=36, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT, size=11, family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
        hovermode="x unified",
        dragmode="pan",
    )
    fig.update_xaxes(showgrid=False, rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def stock_chart(df: pd.DataFrame, name: str, code: str, days: int = 180) -> go.Figure:
    data = df.tail(days).copy()
    data["date"] = pd.to_datetime(data["date"])
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.52, 0.14, 0.20, 0.14],
        subplot_titles=("", "成交量", "MACD (12,26,9)", "RSI14"),
    )
    fig.add_trace(
        go.Candlestick(
            x=data["date"],
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            name="K线",
            increasing_line_color=UP_COLOR,
            decreasing_line_color=DOWN_COLOR,
            increasing_fillcolor=UP_COLOR,
            decreasing_fillcolor=DOWN_COLOR,
        ),
        row=1,
        col=1,
    )
    for column, color, label in [
        ("ma5", "#f6c85f", "MA5"),
        ("ma10", "#4ea8de", "MA10"),
        ("ma20", "#c77dff", "MA20"),
    ]:
        if column in data.columns:
            fig.add_trace(
                go.Scatter(
                    x=data["date"],
                    y=data[column],
                    mode="lines",
                    line=dict(width=1.2, color=color),
                    name=label,
                ),
                row=1,
                col=1,
            )

    colors = [UP_COLOR if c >= o else DOWN_COLOR for o, c in zip(data["open"], data["close"])]
    fig.add_trace(
        go.Bar(x=data["date"], y=data["volume"], marker_color=colors, name="成交量"),
        row=2,
        col=1,
    )
    macd_colors = [UP_COLOR if v >= 0 else DOWN_COLOR for v in data["macd_hist"].fillna(0)]
    fig.add_trace(
        go.Bar(x=data["date"], y=data["macd_hist"], marker_color=macd_colors, name="MACD柱"),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=data["date"], y=data["macd_dif"], mode="lines", line=dict(width=1, color="#f6c85f"), name="DIF"
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=data["date"], y=data["macd_dea"], mode="lines", line=dict(width=1, color="#4ea8de"), name="DEA"
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=data["date"],
            y=data["rsi14"],
            mode="lines",
            line=dict(width=1.2, color="#f59e0b"),
            name="RSI14",
        ),
        row=4,
        col=1,
    )
    fig.add_hline(y=70, line_dash="dot", line_color="rgba(217,72,65,.55)", row=4, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="rgba(31,157,114,.55)", row=4, col=1)
    fig.update_yaxes(range=[0, 100], row=4, col=1)
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_layout(title=dict(text=f"{name}（{code}）日线 · 前复权", x=0.01, font=dict(size=14)))
    return _base_layout(fig, 760)


def breadth_chart(breadth: dict) -> go.Figure:
    labels = ["上涨", "下跌", "平盘"]
    values = [breadth.get("advancers", 0), breadth.get("decliners", 0), breadth.get("unchanged", 0)]
    colors = [UP_COLOR, DOWN_COLOR, "#94a3b8"]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=colors,
            text=[f"{v} 只" for v in values],
            textposition="outside",
            hovertemplate="%{y}: %{x} 只<extra></extra>",
        )
    )
    fig.update_layout(title=dict(text="涨跌家数分布", x=0.01, font=dict(size=13)), showlegend=False)
    fig.update_xaxes(title_text="家数")
    return _base_layout(fig, 230)


def equity_chart(equity: pd.DataFrame) -> go.Figure:
    data = equity.copy()
    data["date"] = pd.to_datetime(data["date"])
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.72, 0.28],
        subplot_titles=("策略净值 vs 沪深300", "策略回撤"),
    )
    fig.add_trace(
        go.Scatter(
            x=data["date"],
            y=data["equity"],
            mode="lines",
            name="策略权益",
            line=dict(color="#f6c85f", width=1.8),
        ),
        row=1,
        col=1,
    )
    if "benchmark_equity" in data.columns:
        fig.add_trace(
            go.Scatter(
                x=data["date"],
                y=data["benchmark_equity"],
                mode="lines",
                name="沪深300",
                line=dict(color="#94a3b8", width=1.2, dash="dot"),
            ),
            row=1,
            col=1,
        )
    peak = data["equity"].cummax()
    dd = (data["equity"] / peak - 1) * 100
    fig.add_trace(
        go.Scatter(
            x=data["date"],
            y=dd,
            mode="lines",
            fill="tozeroy",
            name="回撤%",
            line=dict(color=DOWN_COLOR, width=1),
            fillcolor="rgba(31,157,114,.18)",
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(title_text="权益（元）", row=1, col=1)
    fig.update_yaxes(title_text="%", row=2, col=1)
    return _base_layout(fig, 500)
