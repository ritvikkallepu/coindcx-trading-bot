from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


OUTPUT = Path("outputs/adaptive_hybrid_strategy_brief.pdf")


def paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def bullet_list(items: list[str], style: ParagraphStyle) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(item, style), leftIndent=14) for item in items],
        bulletType="bullet",
        leftIndent=16,
        bulletFontName="Helvetica",
        bulletFontSize=7,
    )


def table(data: list[list[str]], col_widths: list[float]) -> Table:
    tbl = Table(
        [[Paragraph(str(cell), BODY_SMALL) for cell in row] for row in data],
        colWidths=col_widths,
        repeatRows=1,
        hAlign="LEFT",
    )
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return tbl


def section(title: str) -> list:
    return [Spacer(1, 10), Paragraph(title, H2), Spacer(1, 5)]


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawString(0.72 * inch, 0.45 * inch, "CoinDCX Futures Bot - Adaptive Hybrid Strategy Brief")
    canvas.drawRightString(7.78 * inch, 0.45 * inch, f"Page {doc.page}")
    canvas.restoreState()


styles = getSampleStyleSheet()
TITLE = ParagraphStyle(
    "Title",
    parent=styles["Title"],
    fontName="Helvetica-Bold",
    fontSize=24,
    leading=29,
    alignment=TA_CENTER,
    textColor=colors.HexColor("#111827"),
    spaceAfter=10,
)
SUBTITLE = ParagraphStyle(
    "Subtitle",
    parent=styles["BodyText"],
    fontName="Helvetica",
    fontSize=10.5,
    leading=15,
    alignment=TA_CENTER,
    textColor=colors.HexColor("#4b5563"),
    spaceAfter=8,
)
H1 = ParagraphStyle(
    "H1",
    parent=styles["Heading1"],
    fontName="Helvetica-Bold",
    fontSize=15,
    leading=19,
    textColor=colors.HexColor("#111827"),
    spaceBefore=8,
    spaceAfter=6,
)
H2 = ParagraphStyle(
    "H2",
    parent=styles["Heading2"],
    fontName="Helvetica-Bold",
    fontSize=12.5,
    leading=16,
    textColor=colors.HexColor("#0f766e"),
    spaceBefore=5,
    spaceAfter=4,
)
BODY = ParagraphStyle(
    "Body",
    parent=styles["BodyText"],
    fontName="Helvetica",
    fontSize=9.5,
    leading=13.5,
    textColor=colors.HexColor("#111827"),
    alignment=TA_LEFT,
    spaceAfter=5,
)
BODY_SMALL = ParagraphStyle(
    "BodySmall",
    parent=BODY,
    fontSize=8.3,
    leading=11.2,
    spaceAfter=0,
)
CALLOUT = ParagraphStyle(
    "Callout",
    parent=BODY,
    fontName="Helvetica-Bold",
    fontSize=10.2,
    leading=14.5,
    textColor=colors.HexColor("#064e3b"),
    borderColor=colors.HexColor("#99f6e4"),
    borderWidth=0.8,
    borderPadding=8,
    backColor=colors.HexColor("#ecfdf5"),
    spaceBefore=8,
    spaceAfter=10,
)


def build() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=letter,
        rightMargin=0.72 * inch,
        leftMargin=0.72 * inch,
        topMargin=0.72 * inch,
        bottomMargin=0.72 * inch,
        title="Adaptive Hybrid Strategy Brief",
        author="CoinDCX Futures Bot Project",
    )

    story: list = []
    story.append(Spacer(1, 0.25 * inch))
    story.append(paragraph("Adaptive Hybrid Strategy", TITLE))
    story.append(
        paragraph(
            "Detailed explanation of the paper-tested CoinDCX futures strategy used in the trading bot project.",
            SUBTITLE,
        )
    )
    story.append(
        paragraph(
            "Status: Paper research only. This is not live-trading approval, investment advice, or a claim of future profitability.",
            CALLOUT,
        )
    )

    story += section("One-Line Explanation")
    story.append(
        paragraph(
            "The adaptive hybrid strategy switches between trend-following and mean-reversion based on timeframe: it uses EMA-RSI for 1h trend trades, Bollinger-volume reversion for 4h setups, then filters entries using visual structure, open-interest confirmation where available, and conflict checks before the signal reaches the risk manager.",
            BODY,
        )
    )

    story += section("What Problem It Solves")
    story.append(
        paragraph(
            "Early tests showed that a single strategy did not behave well across all timeframes. EMA-RSI was strongest on 1h candles, while Bollinger reversion became more useful on 4h candles. The adaptive hybrid approach avoids forcing one logic everywhere. It selects the engine that best matches the timeframe and then applies extra checks to reduce poor-quality entries.",
            BODY,
        )
    )
    story.append(
        bullet_list(
            [
                "Use trend logic where trend-following historically worked better.",
                "Use mean-reversion logic where wider 4h structure gave cleaner reversal setups.",
                "Block duplicate entries while a position is already open.",
                "Preserve paper-only risk controls before any order reaches execution.",
            ],
            BODY,
        )
    )

    story += section("Strategy Mode Selection")
    story.append(
        table(
            [
                ["Timeframe", "Primary Mode", "Primary Engine", "Secondary Check", "Current Interpretation"],
                ["1h", "Trend", "EMA-RSI crossover", "Bollinger conflict check", "Main candidate timeframe so far, especially on SOL."],
                ["2h", "Trend", "EMA-RSI crossover", "Bollinger conflict check", "Supported by design, not yet deeply validated."],
                ["4h", "Mean reversion", "Bollinger Band + volume", "EMA-RSI conflict check", "Used when BB reversion has cleaner structure."],
                ["1d", "Mean reversion", "Bollinger Band + volume", "EMA-RSI conflict check", "Supported by design, requires longer history validation."],
            ],
            [0.75 * inch, 1.0 * inch, 1.55 * inch, 1.55 * inch, 2.3 * inch],
        )
    )

    story += section("Signal Engine A: EMA-RSI Trend Mode")
    story.append(
        paragraph(
            "This mode comes from the EMA-RSI trend strategy. It is used as the primary engine on trend-oriented timeframes such as 1h and 2h.",
            BODY,
        )
    )
    story.append(
        table(
            [
                ["Component", "Default Setting", "Purpose"],
                ["Fast EMA", "9", "Tracks short-term trend direction."],
                ["Slow EMA", "21", "Tracks slower trend baseline."],
                ["RSI", "14-period RSI", "Confirms whether momentum is strong enough."],
                ["ATR", "14-period ATR", "Sets stop and target distance."],
                ["Stop", "1.5x ATR", "Defines risk distance for trend entries."],
                ["Target", "3.0x ATR", "Targets roughly 2:1 reward versus the ATR stop."],
            ],
            [1.45 * inch, 1.5 * inch, 4.2 * inch],
        )
    )
    story.append(
        bullet_list(
            [
                "Long entry: fast EMA crosses above slow EMA and RSI confirms bullish momentum.",
                "Short entry: fast EMA crosses below slow EMA and RSI confirms bearish momentum.",
                "Exit signals can be generated when the EMA relationship invalidates the existing trend.",
            ],
            BODY,
        )
    )

    story += section("Signal Engine B: Bollinger Volume Reversion Mode")
    story.append(
        paragraph(
            "This mode comes from the Bollinger-volume mean-reversion strategy. It is used as the primary engine on 4h and 1d timeframes, where price extremes and volatility compression can be more meaningful.",
            BODY,
        )
    )
    story.append(
        table(
            [
                ["Component", "Default Setting", "Purpose"],
                ["Bollinger period", "20", "Defines upper, middle, and lower bands."],
                ["Squeeze lookback", "40", "Checks whether current band width is relatively compressed."],
                ["Squeeze threshold", "0.35 rank", "Requires a relatively narrow band environment."],
                ["Volume period", "20", "Computes average volume baseline."],
                ["Volume confirmation", "1.15x average volume", "Avoids weak band touches without participation."],
                ["Stop", "1.2x ATR", "Controls downside on reversion entries."],
                ["Target", "Middle Bollinger Band", "Mean-reversion target back toward the average."],
            ],
            [1.55 * inch, 1.55 * inch, 4.05 * inch],
        )
    )
    story.append(
        bullet_list(
            [
                "Long entry: squeeze condition is present, price tags the lower Bollinger Band, and volume confirms.",
                "Short entry: squeeze condition is present, price tags the upper Bollinger Band, and volume confirms.",
                "The target is usually the middle Bollinger Band rather than an arbitrary fixed percentage.",
            ],
            BODY,
        )
    )

    story.append(PageBreak())
    story.append(paragraph("Filters and Decision Flow", H1))
    story += section("Visual Structure Filter")
    story.append(
        paragraph(
            "The adaptive strategy reuses visual-screen logic from the hybrid meta module. This layer is not a chart screenshot model. It is a programmatic filter that checks recent candle structure and avoids low-quality setups.",
            BODY,
        )
    )
    story.append(
        bullet_list(
            [
                "Blocks entries when the latest volume is too low versus its moving average.",
                "Blocks entries when the latest candle is an extreme spike relative to ATR.",
                "Reviews recent price structure and momentum over a short lookback window.",
                "Acts as an entry quality gate before a trade can reach the risk manager.",
            ],
            BODY,
        )
    )

    story += section("Open Interest Confirmation")
    story.append(
        paragraph(
            "Open interest is built as a confirmation layer. At the moment it only becomes active when open-interest features are supplied to the strategy context. Without OI data, the strategy still operates, but it does not pretend to have OI confirmation.",
            BODY,
        )
    )
    story.append(
        bullet_list(
            [
                "If OI data is present, weak short setups can be blocked unless OI is bearish enough.",
                "OI is not the primary signal; it is used as a confirmation filter.",
                "The next research step is to connect reliable OI ingestion and test how much it improves signal quality.",
            ],
            BODY,
        )
    )

    story += section("Conflict Check")
    story.append(
        paragraph(
            "The adaptive strategy always has a primary engine and a secondary check. If the primary engine says long but the secondary engine says short, the trade is blocked. This protects against strongly contradictory strategy states.",
            BODY,
        )
    )
    story.append(
        table(
            [
                ["Example", "Primary Engine", "Secondary Check", "Decision"],
                ["1h trend mode", "EMA-RSI says enter long", "Bollinger says enter short", "Hold; conflict detected."],
                ["1h trend mode", "EMA-RSI says enter long", "Bollinger says hold", "Entry can proceed if visual/OI/risk allow."],
                ["4h reversion mode", "Bollinger says enter short", "EMA-RSI says enter long", "Hold; conflict detected."],
            ],
            [1.35 * inch, 1.75 * inch, 1.75 * inch, 2.3 * inch],
        )
    )

    story += section("Position Handling")
    story.append(
        paragraph(
            "One important bug/logic improvement was making the backtest strategy position-aware. The strategy can now see whether a paper position is already open and avoids repeatedly firing duplicate entries while that position exists.",
            BODY,
        )
    )
    story.append(
        bullet_list(
            [
                "If no position is open, valid entry signals can proceed.",
                "If a position is already open, the strategy generally holds instead of adding another duplicate entry.",
                "The current default is exit_on_opposite_entry = False. This means the strategy does not instantly close just because the opposite entry appears; stops, targets, and true exit logic do the work.",
                "This default helped preserve the SOL 1h trend edge in paper testing.",
            ],
            BODY,
        )
    )

    story += section("High-Level Decision Flow")
    story.append(
        table(
            [
                ["Step", "Question", "Result"],
                ["1", "What timeframe is being tested?", "Select trend mode for 1h/2h or reversion mode for 4h/1d."],
                ["2", "Does the primary engine produce an entry or exit?", "If no, hold. If yes, continue."],
                ["3", "Is a matching position already open?", "Avoid duplicate entries; manage the existing position."],
                ["4", "Does the visual filter accept the setup?", "If blocked, hold."],
                ["5", "Does OI, if available, confirm weak short setups?", "If not confirmed, block the short."],
                ["6", "Does the secondary strategy disagree?", "If opposite signal appears, hold."],
                ["7", "Does the risk manager approve?", "Only approved paper signals are simulated."],
            ],
            [0.55 * inch, 2.4 * inch, 4.2 * inch],
        )
    )

    story.append(PageBreak())
    story.append(paragraph("Backtest Context and Interpretation", H1))
    story += section("Backtest Assumptions Used")
    story.append(
        table(
            [
                ["Assumption", "Value"],
                ["Trading mode", "Paper trading only"],
                ["Exchange", "CoinDCX futures"],
                ["Starting equity", "1000"],
                ["Leverage", "3x in recent tests"],
                ["Fee rate", "0.0005 per fill, meaning 0.05%"],
                ["Slippage", "0.02%"],
                ["Lookback", "Usually 1000 candles"],
                ["Main candidate pair", "B-SOL_USDT"],
            ],
            [2.15 * inch, 5.0 * inch],
        )
    )

    story += section("Key Paper Results Observed")
    story.append(
        table(
            [
                ["Pair / Timeframe", "Strategy", "Return", "Profit Factor", "Win Rate", "Max Drawdown", "Interpretation"],
                ["SOL 1h", "adaptive_hybrid", "+13.45%", "2.64", "61.11%", "2.99%", "Best candidate so far."],
                ["SOL 1h", "ema_rsi_trend baseline", "+13.05%", "2.24", "57.14%", "4.00%", "Strong baseline; adaptive slightly improved risk/return."],
                ["SOL 4h", "adaptive_hybrid", "+5.92%", "1.55", "56.52%", "4.29%", "Improved over basic BB reversion."],
                ["SOL 4h", "bb_volume_reversion baseline", "+2.28%", "1.18", "50.00%", "4.29%", "Positive but weaker than adaptive."],
                ["BTC 1h", "adaptive_hybrid", "+2.50%", "1.19", "42.86%", "5.22%", "Positive but not strong."],
                ["ETH 1h", "adaptive_hybrid", "+2.43%", "1.19", "42.11%", "5.10%", "Positive but not strong."],
            ],
            [1.0 * inch, 1.1 * inch, 0.7 * inch, 0.8 * inch, 0.75 * inch, 0.85 * inch, 1.95 * inch],
        )
    )

    story += section("Current Verdict")
    story.append(
        paragraph(
            "The adaptive hybrid strategy is a promising paper-research candidate, not a live strategy. Its best current evidence is SOL 1h and SOL 4h. Shorter timeframes such as 1m and 5m have generally looked much weaker because fees, slippage, and noise become more damaging.",
            BODY,
        )
    )

    story += section("Overfitting Risk")
    story.append(
        paragraph(
            "The strategy was influenced by observed test behavior: EMA-RSI worked better on 1h and Bollinger reversion worked better on 4h. That is reasonable engineering, but it creates overfitting risk. We should assume the strategy is not proven until it passes validation that was not used during design.",
            BODY,
        )
    )
    story.append(
        bullet_list(
            [
                "Run out-of-sample tests: tune on older data, validate on newer unseen candles.",
                "Run walk-forward tests across multiple rolling windows.",
                "Run multi-coin sweeps to ensure it does not only work on SOL.",
                "Run parameter sensitivity tests: small changes should not destroy results.",
                "Test across bullish, bearish, sideways, and high-volatility regimes.",
            ],
            BODY,
        )
    )

    story += section("How to Explain It to Someone")
    story.append(
        paragraph(
            "This bot strategy does not blindly trade every indicator signal. It first chooses the right style of strategy for the timeframe: trend-following on 1h, mean-reversion on 4h. Then it checks whether the chart structure, volume, open-interest confirmation, and secondary strategy agree enough to justify a trade. After that, the signal still has to pass the risk manager. In short: it is an adaptive strategy selector with risk-first execution discipline.",
            CALLOUT,
        )
    )

    story += section("Files in the Project")
    story.append(
        table(
            [
                ["File", "Role"],
                ["app/strategies/adaptive_hybrid.py", "Main adaptive strategy selector."],
                ["app/strategies/ema_rsi_trend.py", "Trend-following EMA-RSI engine."],
                ["app/strategies/bb_volume_reversion.py", "Bollinger-volume mean-reversion engine."],
                ["app/strategies/hybrid_meta.py", "Visual screen and open-interest scoring utilities."],
                ["app/backtest/engine.py", "Paper backtest engine that passes open-position context."],
                ["app/risk/manager.py", "Paper risk approval before execution."],
            ],
            [2.55 * inch, 4.6 * inch],
        )
    )

    story += section("Important Disclaimer")
    story.append(
        paragraph(
            "This document describes a software strategy and its paper backtest behavior. It is not financial advice. Futures trading is high risk, leverage can magnify losses, and paper-tested results can fail in live markets due to liquidity, latency, fees, slippage, API failures, liquidation mechanics, and changing market regimes.",
            BODY,
        )
    )

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


if __name__ == "__main__":
    build()
    print(OUTPUT.resolve())
