from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from app.config import Settings
from app.risk.models import convert_for_json
from app.research.excel import write_sweep_workbook
from app.research.sweep import (
    SweepConfig,
    StrategyVariant,
    run_research_sweep,
    strategy_variants_for_names,
)


@dataclass(frozen=True)
class QuickValidationConfig:
    base_sweep: SweepConfig
    output_dir: Path = Path("research/backtests/quick_validator")
    quick_lookback: int = 240
    validation_lookback: int = 500
    final_lookback: int = 1000
    quick_max_runs: int = 60
    top_validation: int = 8
    top_final: int = 3
    quick_only: bool = False

    def __post_init__(self) -> None:
        if self.quick_lookback <= 0:
            raise ValueError("quick_lookback must be positive.")
        if self.validation_lookback <= 0:
            raise ValueError("validation_lookback must be positive.")
        if self.final_lookback <= 0:
            raise ValueError("final_lookback must be positive.")
        if self.quick_max_runs <= 0:
            raise ValueError("quick_max_runs must be positive.")
        if self.top_validation < 0:
            raise ValueError("top_validation cannot be negative.")
        if self.top_final < 0:
            raise ValueError("top_final cannot be negative.")


def run_quick_validator(
    *,
    settings: Settings,
    config: QuickValidationConfig,
    variants: tuple[StrategyVariant, ...],
    logger: logging.Logger | None = None,
) -> dict[str, object]:
    log = logger or logging.getLogger(__name__)
    started_at = datetime.now().astimezone()
    root_dir = config.output_dir / started_at.strftime("quick_validator_%Y%m%d_%H%M%S")
    root_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, object]] = []
    stage_outputs: list[dict[str, object]] = []
    manifest_path = root_dir / "manifest.json"

    _write_manifest(
        manifest_path,
        _manifest(
            config=config,
            root_dir=root_dir,
            status="running",
            started_at=started_at,
            stage_outputs=stage_outputs,
            top_results=[],
        ),
    )

    quick_output = _run_stage(
        settings=settings,
        stage="quick",
        stage_dir=root_dir / "01_quick",
        sweep_config=replace(
            config.base_sweep,
            lookback=config.quick_lookback,
            output_dir=root_dir / "01_quick",
            max_runs=config.quick_max_runs,
        ),
        variants=variants,
        logger=log,
    )
    stage_outputs.append(quick_output)
    quick_rows = _rows_from_output(quick_output, stage="quick")
    all_rows.extend(quick_rows)

    validation_rows: list[dict[str, object]] = []
    if not config.quick_only and config.top_validation > 0:
        selected_for_validation = _select_top_rows(quick_rows, limit=config.top_validation)
        validation_rows = _run_selected_stage(
            settings=settings,
            config=config,
            selected_rows=selected_for_validation,
            variants=variants,
            stage="validation",
            stage_index=2,
            lookback=config.validation_lookback,
            stage_dir=root_dir / "02_validation",
            logger=log,
        )
        all_rows.extend(validation_rows)
        stage_outputs.append(
            _combined_stage_output(
                root_dir=root_dir,
                stage="validation",
                rows=validation_rows,
                config=config,
            )
        )

    final_rows: list[dict[str, object]] = []
    if not config.quick_only and config.top_final > 0:
        source_rows = validation_rows if validation_rows else quick_rows
        selected_for_final = _select_top_rows(source_rows, limit=config.top_final)
        final_rows = _run_selected_stage(
            settings=settings,
            config=config,
            selected_rows=selected_for_final,
            variants=variants,
            stage="final",
            stage_index=3,
            lookback=config.final_lookback,
            stage_dir=root_dir / "03_final",
            logger=log,
        )
        all_rows.extend(final_rows)
        stage_outputs.append(
            _combined_stage_output(
                root_dir=root_dir,
                stage="final",
                rows=final_rows,
                config=config,
            )
        )

    summary_csv = root_dir / "quick_validator_summary.csv"
    workbook_xlsx = root_dir / "quick_validator_results.xlsx"
    top_results = _select_top_rows(
        final_rows or validation_rows or quick_rows,
        limit=10,
    )
    _write_dynamic_csv(summary_csv, all_rows)
    write_sweep_workbook(
        workbook_xlsx,
        rows=all_rows,
        assumptions=_assumptions(config=config, status="finished"),
    )

    ended_at = datetime.now().astimezone()
    result = _manifest(
        config=config,
        root_dir=root_dir,
        status="finished",
        started_at=started_at,
        stage_outputs=stage_outputs,
        top_results=top_results,
    )
    result["ended_at"] = ended_at.isoformat()
    result["summary_csv"] = str(summary_csv)
    result["workbook_xlsx"] = str(workbook_xlsx)
    result["completed_rows"] = len([row for row in all_rows if not row.get("error")])
    result["error_rows"] = len([row for row in all_rows if row.get("error")])
    _write_manifest(manifest_path, result)
    return result


def _run_stage(
    *,
    settings: Settings,
    stage: str,
    stage_dir: Path,
    sweep_config: SweepConfig,
    variants: tuple[StrategyVariant, ...],
    logger: logging.Logger,
) -> dict[str, object]:
    logger.info(
        "Starting %s validation stage: lookback=%s max_runs=%s",
        stage,
        sweep_config.lookback,
        sweep_config.max_runs,
    )
    result = run_research_sweep(
        settings=settings,
        config=sweep_config,
        variants=variants,
        logger=logger,
    )
    return {
        "stage": stage,
        "requested_output_dir": str(stage_dir),
        **result,
    }


def _run_selected_stage(
    *,
    settings: Settings,
    config: QuickValidationConfig,
    selected_rows: list[dict[str, object]],
    variants: tuple[StrategyVariant, ...],
    stage: str,
    stage_index: int,
    lookback: int,
    stage_dir: Path,
    logger: logging.Logger,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    stage_dir.mkdir(parents=True, exist_ok=True)
    known_variants = {variant.name: variant for variant in variants}
    for rank, row in enumerate(selected_rows, start=1):
        variant_name = str(row.get("variant") or "")
        selected_variant = known_variants.get(variant_name)
        if selected_variant is None:
            selected_variant = strategy_variants_for_names((variant_name,))[0]

        sweep_config = _sweep_config_from_row(
            base=config.base_sweep,
            row=row,
            lookback=lookback,
            output_dir=stage_dir / f"{rank:02d}_{_row_slug(row)}",
        )
        output = _run_stage(
            settings=settings,
            stage=f"{stage}_{rank:02d}",
            stage_dir=stage_dir,
            sweep_config=sweep_config,
            variants=(selected_variant,),
            logger=logger,
        )
        rows.extend(_rows_from_output(output, stage=stage, source_rank=rank))
    return rows


def _sweep_config_from_row(
    *,
    base: SweepConfig,
    row: dict[str, object],
    lookback: int,
    output_dir: Path,
) -> SweepConfig:
    return replace(
        base,
        pairs=(str(row["pair"]),),
        intervals=(str(row["interval"]),),
        lookback=lookback,
        leverage=_decimal(row.get("leverage"), base.leverage),
        risk_per_trade_pct=_optional_decimal(
            row.get("risk_per_trade_pct"),
            base.risk_per_trade_pct,
        ),
        take_profit_pct=_optional_decimal(row.get("take_profit_pct"), base.take_profit_pct),
        trailing_stop_enabled=_bool(
            row.get("trailing_stop_enabled"),
            base.trailing_stop_enabled,
        ),
        trailing_stop_activation_pct=_decimal(
            row.get("trailing_stop_activation_pct"),
            base.trailing_stop_activation_pct,
        ),
        trailing_stop_distance_pct=_decimal(
            row.get("trailing_stop_distance_pct"),
            base.trailing_stop_distance_pct,
        ),
        atr_dynamic_exits_enabled=_bool(
            row.get("atr_dynamic_exits_enabled"),
            base.atr_dynamic_exits_enabled,
        ),
        risk_per_trade_pct_values=(),
        leverage_values=(),
        take_profit_pct_values=(),
        trailing_profiles=(),
        atr_dynamic_exits_values=(),
        output_dir=output_dir,
        max_runs=1,
    )


def _rows_from_output(
    output: dict[str, object],
    *,
    stage: str,
    source_rank: int | None = None,
) -> list[dict[str, object]]:
    summary_path = output.get("summary_csv")
    if not summary_path:
        return []
    rows = _read_summary_rows(Path(str(summary_path)))
    for row in rows:
        row["stage"] = stage
        row["source_rank"] = "" if source_rank is None else source_rank
    return rows


def _read_summary_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _select_top_rows(
    rows: Iterable[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    eligible = [row for row in rows if not row.get("error")]
    eligible.sort(key=_ranking_tuple, reverse=True)
    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    for row in eligible:
        key = (
            str(row.get("pair", "")),
            str(row.get("interval", "")),
            str(row.get("variant", "")),
            str(row.get("risk_per_trade_pct", "")),
            str(row.get("leverage", "")),
            str(row.get("take_profit_pct", "")),
            str(row.get("trailing_profile", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _ranking_tuple(row: dict[str, object]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    return (
        _decimal(row.get("rank_score"), Decimal("-999999")),
        _decimal(row.get("profit_factor"), Decimal("0")),
        _decimal(row.get("total_return_pct"), Decimal("-999999")),
        _decimal(row.get("trade_count"), Decimal("0")),
    )


def _combined_stage_output(
    *,
    root_dir: Path,
    stage: str,
    rows: list[dict[str, object]],
    config: QuickValidationConfig,
) -> dict[str, object]:
    summary_csv = root_dir / f"{stage}_summary.csv"
    workbook_xlsx = root_dir / f"{stage}_results.xlsx"
    _write_dynamic_csv(summary_csv, rows)
    write_sweep_workbook(
        workbook_xlsx,
        rows=rows,
        assumptions=_assumptions(config=config, status=f"{stage}_finished"),
    )
    return {
        "stage": stage,
        "summary_csv": str(summary_csv),
        "workbook_xlsx": str(workbook_xlsx),
        "completed": len([row for row in rows if not row.get("error")]),
        "errors": len([row for row in rows if row.get("error")]),
    }


def _write_dynamic_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    if not headers:
        headers = ["status"]
        rows = [{"status": "no_rows"}]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(convert_for_json(row))


def _write_manifest(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(convert_for_json(value), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _manifest(
    *,
    config: QuickValidationConfig,
    root_dir: Path,
    status: str,
    started_at: datetime,
    stage_outputs: list[dict[str, object]],
    top_results: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "status": status,
        "started_at": started_at.isoformat(),
        "output_dir": str(root_dir),
        "quick_lookback": config.quick_lookback,
        "validation_lookback": config.validation_lookback,
        "final_lookback": config.final_lookback,
        "quick_max_runs": config.quick_max_runs,
        "top_validation": config.top_validation,
        "top_final": config.top_final,
        "quick_only": config.quick_only,
        "stage_outputs": stage_outputs,
        "top_results": top_results,
    }


def _assumptions(config: QuickValidationConfig, *, status: str) -> dict[str, object]:
    base = config.base_sweep
    return {
        "status": status,
        "mode": "progressive quick validator",
        "pairs": ", ".join(base.pairs),
        "intervals": ", ".join(base.intervals),
        "quick_lookback": config.quick_lookback,
        "validation_lookback": config.validation_lookback,
        "final_lookback": config.final_lookback,
        "quick_max_runs": config.quick_max_runs,
        "top_validation": config.top_validation,
        "top_final": config.top_final,
        "starting_equity": base.starting_equity,
        "risk_grid": ", ".join(str(value) for value in base.risk_per_trade_pct_values),
        "leverage_grid": ", ".join(str(value) for value in base.leverage_values),
        "take_profit_grid": ", ".join(
            "default" if value is None else str(value)
            for value in base.take_profit_pct_values
        ),
        "trailing_grid": ", ".join(profile.label for profile in base.trailing_profiles),
        "atr_grid": ", ".join("on" if value else "off" for value in base.atr_dynamic_exits_values),
        "notes": (
            "Fast screen first, then re-tests only the best candidates on larger "
            "lookbacks. Use this to find candidates for deeper validation, not as "
            "live-trading approval."
        ),
    }


def _row_slug(row: dict[str, object]) -> str:
    text = "_".join(
        str(row.get(key, ""))
        for key in ("pair", "interval", "variant", "risk_per_trade_pct", "leverage")
    )
    safe = []
    for char in text:
        safe.append(char if char.isalnum() else "_")
    return "".join(safe).strip("_")[:80] or "candidate"


def _optional_decimal(value: object, default: Decimal | None) -> Decimal | None:
    if value is None:
        return default
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "default", "strategy"}:
        return default
    return _decimal(value, default if default is not None else Decimal("0"))


def _decimal(value: object, default: Decimal) -> Decimal:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        return default


def _bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default
