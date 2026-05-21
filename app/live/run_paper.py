from __future__ import annotations

import os
import logging
from app.config import load_settings
from app.live.paper_loop import PaperTradingLoop


def main() -> None:
    settings = load_settings()
    
    # Configure root logging for live trading visibility
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    pair = os.environ.get("PAPER_PAIR", "B-BTC_USDT")
    interval = os.environ.get("PAPER_INTERVAL", "15m")
    strategy_name = os.environ.get("PAPER_STRATEGY", "adaptive_hybrid")
    
    loop = PaperTradingLoop(settings, strategy_name=strategy_name)
    
    from app.live.paper_loop import set_active_loop
    set_active_loop(loop)

    # PaperTradingLoop.run() is synchronous — the websocket client blocks internally.
    loop.run(pair, interval)


if __name__ == "__main__":
    main()
