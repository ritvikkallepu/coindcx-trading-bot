from __future__ import annotations

try:
    import pytest
except ModuleNotFoundError:  # Allows unittest discovery in the bundled runtime.
    class _PytestFallback:
        @staticmethod
        def fixture(func=None, **kwargs):
            if func is None:
                return lambda wrapped: wrapped
            return func

    pytest = _PytestFallback()
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.strategies.hybrid_meta import HybridMetaStrategy
from app.strategies.base import StrategyContext, SignalDirection
from app.config import Settings
from app.risk.manager import RiskManager
from app.risk.models import OpenPosition
from app.risk.limits import is_entry_signal

@pytest.fixture
def strategy():
    return HybridMetaStrategy()

@pytest.fixture
def base_config():
    return {
        "intrabar_reversal_breakout_enabled": True,
        "reversal_breakout_volume_ratio": Decimal("0.5"), # Low for testing
        "reversal_breakout_body_ratio": Decimal("0.5"),
        "reversal_breakout_close_position_ratio": Decimal("0.5"),
        "reversal_breakout_max_extension_atr": Decimal("5.0"),
    }

from dataclasses import replace

def test_breakout_uses_previous_parent_high(strategy, base_config):
    # Previous parent high = 100
    # Current parent high (partial) = 110 (this includes our current 1m spike)
    # Execution candle close = 105
    # If we used current parent high, 105 <= 110 -> REJECTED
    # If we use previous parent high, 105 > 100 -> ACCEPTED
    
    candles = []
    for i in range(60):
        candles.append(OHLCVCandle(
            pair="B-AXL_USDT", interval="5m", open_time_ms=i*300000, close_time_ms=(i+1)*300000-1,
            open=Decimal("90"), high=Decimal("92"), low=Decimal("88"), close=Decimal("90"), volume=Decimal("100")
        ))
    
    # Replace last one with our specific test case
    prev_closed = OHLCVCandle(
        pair="B-AXL_USDT", interval="5m", open_time_ms=60*300000, close_time_ms=61*300000-1,
        open=Decimal("90"), high=Decimal("100"), low=Decimal("85"), close=Decimal("95"), volume=Decimal("1000")
    )
    current_partial = OHLCVCandle(
        pair="B-AXL_USDT", interval="5m", open_time_ms=61*300000, close_time_ms=62*300000-1,
        open=Decimal("95"), high=Decimal("110"), low=Decimal("95"), close=Decimal("105"), volume=Decimal("500")
    )
    
    candles.append(prev_closed)
    candles.append(current_partial)
    
    exec_candles = [
        OHLCVCandle(pair="B-AXL_USDT", interval="1m", open_time_ms=61*300000, close_time_ms=61*300000+59999, open=Decimal("95"), high=Decimal("96"), low=Decimal("94"), close=Decimal("95"), volume=Decimal("100")),
        OHLCVCandle(pair="B-AXL_USDT", interval="1m", open_time_ms=61*300000+60000, close_time_ms=61*300000+119999, open=Decimal("95"), high=Decimal("97"), low=Decimal("95"), close=Decimal("96"), volume=Decimal("100")),
        OHLCVCandle(pair="B-AXL_USDT", interval="1m", open_time_ms=61*300000+120000, close_time_ms=61*300000+179999, open=Decimal("96"), high=Decimal("106"), low=Decimal("96"), close=Decimal("105"), volume=Decimal("500"))
    ]
    
    config = {**base_config, "previous_parent_high": Decimal("100")}
    context = StrategyContext(
        pair="B-AXL_USDT",
        interval="5m",
        candles=CandleSeries(candles),
        indicators=MagicMock(atr=Decimal("1.0"), fast_ema=Decimal("95")),
        features={
            "backtest_config": config,
            "execution_candles": exec_candles
        }
    )
    
    signal = strategy.evaluate(context)
    # Check that it's a breakout, but it might be momentum_ignition or balanced_breakout
    assert signal.metadata.get("entry_type") == "intrabar_reversal_breakout" or signal.metadata.get("entry_type") == "balanced_breakout"
    assert signal.direction == SignalDirection.LONG
    assert signal.metadata["previous_parent_high"] == Decimal("100")

from app.strategies.base import StrategySignal, SignalAction

def test_multi_pair_entry_allowed(base_config):
    # Test that BSB short doesn't block AXL long entry
    settings = Settings()
    new_risk = replace(settings.risk, max_open_positions=2, allow_multi_pair_positions=True, allow_same_pair_pyramiding=False)
    settings = replace(settings, risk=new_risk)
    
    risk_manager = RiskManager(settings.risk)
    
    axl_signal = StrategySignal(
        strategy_name="hybrid_meta_v2",
        pair="B-AXL_USDT",
        interval="5m",
        action=SignalAction.ENTER_LONG,
        confidence=Decimal("0.8"),
        reason="Breakout",
        timestamp_ms=1000,
        direction=SignalDirection.LONG,
        entry_price=Decimal("0.0626"),
        stop_loss=Decimal("0.0600"),
        metadata={"entry_type": "intrabar_reversal_breakout"}
    )
    
    bsb_position = OpenPosition(
        pair="B-BSB_USDT",
        direction=SignalDirection.SHORT,
        quantity=Decimal("100"),
        entry_price=Decimal("1.0"),
        leverage=Decimal("5"),
        stop_loss=Decimal("1.1")
    )
    
    decision = risk_manager.evaluate_signal(
        axl_signal,
        account_equity=Decimal("1000"),
        available_equity=Decimal("800"),
        open_positions=(bsb_position,),
        daily_realized_pnl=Decimal("0"),
        trading_mode="paper",
        live_trading_enabled=False,
        requested_leverage=Decimal("5")
    )
    
    assert decision.approved is True

def test_same_pair_block_still_works(base_config):
    settings = Settings()
    new_risk = replace(settings.risk, max_open_positions=2, allow_multi_pair_positions=True, allow_same_pair_pyramiding=False)
    settings = replace(settings, risk=new_risk)
    
    risk_manager = RiskManager(settings.risk)
    
    axl_signal = StrategySignal(
        strategy_name="hybrid_meta_v2",
        pair="B-AXL_USDT",
        interval="5m",
        action=SignalAction.ENTER_LONG,
        confidence=Decimal("0.8"),
        reason="Breakout",
        timestamp_ms=1000,
        direction=SignalDirection.LONG,
        metadata={}
    )
    
    axl_existing = OpenPosition(
        pair="B-AXL_USDT",
        direction=SignalDirection.LONG,
        quantity=Decimal("100"),
        entry_price=Decimal("0.06"),
        leverage=Decimal("5")
    )
    
    decision = risk_manager.evaluate_signal(
        axl_signal,
        account_equity=Decimal("1000"),
        available_equity=Decimal("800"),
        open_positions=(axl_existing,),
        daily_realized_pnl=Decimal("0"),
        trading_mode="paper",
        live_trading_enabled=False,
        requested_leverage=Decimal("5")
    )
    
    assert decision.approved is False
    assert "same_pair_position_blocked" in decision.reason

def test_breakout_rejection_reasons(strategy, base_config):
    # Test Volume too low
    config = {**base_config, "previous_parent_high": Decimal("100"), "reversal_breakout_volume_ratio": Decimal("10.0")}
    
    prev_closed = OHLCVCandle(
        pair="B-AXL_USDT", interval="5m", open_time_ms=0, close_time_ms=299999,
        open=Decimal("90"), high=Decimal("100"), low=Decimal("85"), close=Decimal("95"), volume=Decimal("1000")
    )
    exec_candles = [
        OHLCVCandle(pair="B-AXL_USDT", interval="1m", open_time_ms=300000, close_time_ms=359999, open=Decimal("95"), high=Decimal("96"), low=Decimal("94"), close=Decimal("95"), volume=Decimal("100")),
        OHLCVCandle(pair="B-AXL_USDT", interval="1m", open_time_ms=360000, close_time_ms=419999, open=Decimal("95"), high=Decimal("97"), low=Decimal("95"), close=Decimal("96"), volume=Decimal("100")),
        OHLCVCandle(pair="B-AXL_USDT", interval="1m", open_time_ms=420000, close_time_ms=479999, open=Decimal("96"), high=Decimal("106"), low=Decimal("96"), close=Decimal("105"), volume=Decimal("100"))
    ]
    
    context = StrategyContext(
        pair="B-AXL_USDT",
        interval="5m",
        candles=CandleSeries([prev_closed]),
        indicators=MagicMock(atr=Decimal("1.0"), fast_ema=Decimal("95")),
        features={
            "backtest_config": config,
            "execution_candles": exec_candles
        }
    )
    
    # We need to capture the rejection reason. Our strategy now updates a metadata dict we pass in, 
    # but evaluate() creates its own metadata. 
    # Actually, the strategy's evaluation logic for breakout is called internally.
    # To test rejection reasons, we can mock the metadata dict or check if the strategy 
    # can be made to return the reason.
    
    # Let's check how evaluate handles it.
    signals = strategy.evaluate(context)
    # If rejected, it returns no breakout signal.
    # But wait, my implementation of _intrabar_reversal_breakout_signal updates parent_metadata.
    # In evaluate(), parent_metadata is usually created locally.
    
    # Let's look at evaluate() in hybrid_meta.py.
    # It calls _intrabar_reversal_breakout_signal(context=context, parent_metadata=metadata, ...)
    # If it returns None, we don't see the metadata unless we are in the same scope.
    
    # However, for testing, we can call the private method directly.
    parent_metadata = {}
    signal = strategy._intrabar_reversal_breakout_signal(
        context=context,
        parent_metadata=parent_metadata,
        atr=Decimal("1.0"),
        fast_ema=Decimal("95"),
        ema_score=Decimal("0.5"),
        visual_score=Decimal("0.5"),
        final_score=Decimal("0.5")
    )
    
    assert signal is None
    # Flexible check for the new granular rejection reasons
    rejection = parent_metadata.get("breakout_rejection", "")
    assert "volume_ratio_too_low" in rejection or "false_breakout_low_volume" in rejection
