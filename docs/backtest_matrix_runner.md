# Headless Batch Backtest Runner

Automates the execution of multiple backtest configurations defined in an Excel workbook.

## Setup

1. Place your matrix workbook at `backtest_matrices/systematic_backtest_matrix_current_bot.xlsx`.
2. Ensure it has a sheet named `Backtest Queue`.
3. The sheet should have input columns like `pair`, `interval`, `strategy`, `lookback`, etc.

## Usage

### Run all pending rows
```bash
python scripts/run_backtest_matrix.py --workbook backtest_matrices/systematic_backtest_matrix_current_bot.xlsx
```

### Resume an interrupted run
```bash
python scripts/run_backtest_matrix.py --workbook backtest_matrices/systematic_backtest_matrix_current_bot.xlsx --resume
```
This skips rows marked as `Complete`.

### Run only the first 10 rows
```bash
python scripts/run_backtest_matrix.py --workbook backtest_matrices/systematic_backtest_matrix_current_bot.xlsx --limit 10
```

### Retry failed rows
```bash
python scripts/run_backtest_matrix.py --workbook backtest_matrices/systematic_backtest_matrix_current_bot.xlsx --retry-failed
```

### Run only for a specific coin
```bash
python scripts/run_backtest_matrix.py --workbook backtest_matrices/systematic_backtest_matrix_current_bot.xlsx --coin BTC-USDT
```

### Dry run (no execution)
```bash
python scripts/run_backtest_matrix.py --workbook backtest_matrices/systematic_backtest_matrix_current_bot.xlsx --dry-run
```

## Features

- **Candle Caching**: Reuses candle data for rows with the same `pair`, `interval`, and `lookback` during a single session.
- **Safety**: Wait between runs (`--sleep 0.5`) to avoid rate limits.
- **Resiliency**: Saves the workbook after every row. If one row fails, it marks it `Failed` and continues with the next.
- **Run Quality**: Uses the same logic as the dashboard to classify runs (e.g., "Strong Paper Run", "Watchlist").

## Important Note

**Do not keep the Excel file open** while the script is running. Most OS file locks will prevent the script from saving results back to the workbook if it's open in another application.
