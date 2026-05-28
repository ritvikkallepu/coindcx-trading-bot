from __future__ import annotations

from app.strategies.adaptive_hybrid import AdaptiveHybridStrategy
from app.strategies.base import StrategyEngine
from app.strategies.bb_dynamic_grid import BollingerDynamicFuturesGridStrategy
from app.strategies.bb_volume_reversion import BollingerVolumeMeanReversionStrategy
from app.strategies.ema_rsi_trend import EMARSICrossoverStrategy
from app.strategies.hybrid_meta import HybridMetaStrategy, HybridMetaV2Strategy
from app.strategies.rsi_macd_momentum import RSIMACDMomentumStrategy


STRATEGY_CHOICES = (
    "all",
    "ema_rsi_trend",
    "bb_volume_reversion",
    "hybrid_meta",
    "hybrid_meta_v2",
    "rsi_macd_momentum",
    "adaptive_hybrid",
    "bb_dynamic_grid",
)


def default_strategy_engine() -> StrategyEngine:
    return StrategyEngine(
        [
            EMARSICrossoverStrategy(),
            BollingerVolumeMeanReversionStrategy(),
        ]
    )


def all_strategy_engine() -> StrategyEngine:
    return StrategyEngine(
        [
            EMARSICrossoverStrategy(),
            BollingerVolumeMeanReversionStrategy(),
            HybridMetaStrategy(),
            HybridMetaV2Strategy(),
            RSIMACDMomentumStrategy(),
            AdaptiveHybridStrategy(),
            BollingerDynamicFuturesGridStrategy(),
        ]
    )


def strategy_engine_for_name(name: str) -> StrategyEngine:
    normalized = name.strip().lower()
    if normalized == "all":
        return all_strategy_engine()
    if normalized == "ema_rsi_trend":
        return StrategyEngine([EMARSICrossoverStrategy()])
    if normalized == "bb_volume_reversion":
        return StrategyEngine([BollingerVolumeMeanReversionStrategy()])
    if normalized == "hybrid_meta":
        return StrategyEngine([HybridMetaStrategy()])
    if normalized == "hybrid_meta_v2":
        return StrategyEngine([HybridMetaV2Strategy()])
    if normalized in {"rsi_macd_momentum", "rsi_macd"}:
        return StrategyEngine([RSIMACDMomentumStrategy()])
    if normalized == "adaptive_hybrid":
        return StrategyEngine([AdaptiveHybridStrategy()])
    if normalized == "bb_dynamic_grid":
        return StrategyEngine([BollingerDynamicFuturesGridStrategy()])
    raise ValueError(
        f"Unknown strategy {name}. Choose one of: {', '.join(STRATEGY_CHOICES)}"
    )
