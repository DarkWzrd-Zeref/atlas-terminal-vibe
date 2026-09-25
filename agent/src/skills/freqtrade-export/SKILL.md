---
name: freqtrade-export
description: Produce an explicit native Freqtrade IStrategy bundle for owner review and Kraken spot backtesting in Atlas Terminal.
category: tool
---

Use when the user asks to transfer research into Atlas Terminal's Freqtrade Strategy Lab. Preserve the existing run, original code/signal_engine.py, results and research report. Never claim a Vibe portfolio-weight model is automatically equivalent to a Freqtrade entry/exit strategy.

1. Read the user's rules and the existing run's config.json and code/signal_engine.py when present. Identify candle interval, market, lookback, sizing, entries, exits and risk controls.
2. Write a real Python subclass of freqtrade.strategy.IStrategy (INTERFACE_VERSION = 3) under artifacts/freqtrade/. Use native populate_indicators, populate_entry_trend and populate_exit_trend methods; keep stoploss, ROI and startup candles explicit. This deployment uses Kraken spot: can_short = False. Use only past/current candles, never future shifts or whole-series future statistics. No exchange credentials, network side effects or installation commands in a strategy.
3. Preserve supporting Python packages, parameter JSON and any required files under the same artifact directory, with their original relative layout. Declare extra dependencies for a separately reviewed image update; exporting does not install them.
4. Write artifacts/freqtrade/strategy.json with exactly these fields:

```json
{
  "strategy_class": "MyStrategy",
  "timeframe": "5m",
  "entrypoint": "MyStrategy.py",
  "semantic_notes": "Explain how original weights became entries, exits and sizing; list all differences and unsupported behavior.",
  "dependencies": []
}
```

5. Report the run ID. The Terminal export reader computes file hashes and prepares an inert bundle. The owner reviews the exact files before sending them to the isolated Strategy Lab and explicitly starts a native backtest. Do not claim success until Freqtrade actually loads the strategy and completes its test. A profitable research result, valid Python, or an export file is not proof of native compatibility.

If the original strategy cannot be represented faithfully, state the missing behavior and ask the user to choose the intended trading semantics. Do not silently drop portfolio weighting, cross-market dependencies, leverage or rebalance rules. Do not overwrite the original research to manufacture an apparent match. Paper promotion is a later explicit owner action after validation; live execution is outside this workflow.
