from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from math import sqrt
from typing import Iterable

from app.data.candle_builder import CandleSeries, OHLCVCandle


@dataclass(frozen=True)
class BollingerBandPoint:
    middle: Decimal
    upper: Decimal
    lower: Decimal
    width_pct: Decimal


@dataclass(frozen=True)
class MACDPoint:
    macd: Decimal
    signal: Decimal
    histogram: Decimal


@dataclass(frozen=True)
class VolumeProfileBin:
    low: Decimal
    high: Decimal
    volume: Decimal


@dataclass(frozen=True)
class IndicatorSnapshot:
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    rsi: Decimal | None
    macd: MACDPoint | None
    bollinger: BollingerBandPoint | None
    atr: Decimal | None
    volume_profile: list[VolumeProfileBin]

    def to_dict(self) -> dict[str, object]:
        def convert(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            if hasattr(value, "__dataclass_fields__"):
                return {key: convert(item) for key, item in asdict(value).items()}
            return value

        return convert(self)


def simple_moving_average(values: Iterable[Decimal], period: int) -> list[Decimal | None]:
    _validate_period(period)
    series = list(values)
    result: list[Decimal | None] = []
    running = Decimal("0")

    for index, value in enumerate(series):
        running += value
        if index >= period:
            running -= series[index - period]
        if index + 1 >= period:
            result.append(running / Decimal(period))
        else:
            result.append(None)
    return result


def exponential_moving_average(
    values: Iterable[Decimal],
    period: int,
) -> list[Decimal | None]:
    _validate_period(period)
    series = list(values)
    if not series:
        return []

    multiplier = Decimal("2") / Decimal(period + 1)
    result: list[Decimal | None] = []
    ema: Decimal | None = None

    for index, value in enumerate(series):
        if index + 1 < period:
            result.append(None)
            continue
        if index + 1 == period:
            ema = sum(series[:period], Decimal("0")) / Decimal(period)
        else:
            assert ema is not None
            ema = ((value - ema) * multiplier) + ema
        result.append(ema)

    return result


def relative_strength_index(
    values: Iterable[Decimal],
    period: int = 14,
) -> list[Decimal | None]:
    _validate_period(period)
    closes = list(values)
    if len(closes) < 2:
        return [None] * len(closes)

    result: list[Decimal | None] = [None] * len(closes)
    gains: list[Decimal] = []
    losses: list[Decimal] = []

    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, Decimal("0")))
        losses.append(abs(min(change, Decimal("0"))))

    if len(gains) < period:
        return result

    avg_gain = sum(gains[:period], Decimal("0")) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal("0")) / Decimal(period)
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(closes)):
        gain = gains[index - 1]
        loss = losses[index - 1]
        avg_gain = ((avg_gain * Decimal(period - 1)) + gain) / Decimal(period)
        avg_loss = ((avg_loss * Decimal(period - 1)) + loss) / Decimal(period)
        result[index] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def macd(
    values: Iterable[Decimal],
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> list[MACDPoint | None]:
    _validate_period(fast_period)
    _validate_period(slow_period)
    _validate_period(signal_period)
    if fast_period >= slow_period:
        raise ValueError("fast_period must be smaller than slow_period.")

    closes = list(values)
    fast = exponential_moving_average(closes, fast_period)
    slow = exponential_moving_average(closes, slow_period)
    macd_line: list[Decimal | None] = [
        fast_value - slow_value
        if fast_value is not None and slow_value is not None
        else None
        for fast_value, slow_value in zip(fast, slow)
    ]
    compact_macd = [value for value in macd_line if value is not None]
    signal_compact = exponential_moving_average(compact_macd, signal_period)

    signal_by_index: list[Decimal | None] = [None] * len(closes)
    compact_index = 0
    for index, value in enumerate(macd_line):
        if value is None:
            continue
        signal_by_index[index] = signal_compact[compact_index]
        compact_index += 1

    result: list[MACDPoint | None] = []
    for macd_value, signal_value in zip(macd_line, signal_by_index):
        if macd_value is None or signal_value is None:
            result.append(None)
        else:
            result.append(
                MACDPoint(
                    macd=macd_value,
                    signal=signal_value,
                    histogram=macd_value - signal_value,
                )
            )
    return result


def bollinger_bands(
    values: Iterable[Decimal],
    *,
    period: int = 20,
    stddev_multiplier: Decimal = Decimal("2"),
) -> list[BollingerBandPoint | None]:
    _validate_period(period)
    closes = list(values)
    result: list[BollingerBandPoint | None] = []

    for index in range(len(closes)):
        if index + 1 < period:
            result.append(None)
            continue
        window = closes[index + 1 - period : index + 1]
        mean = sum(window, Decimal("0")) / Decimal(period)
        variance = sum((value - mean) ** 2 for value in window) / Decimal(period)
        stdev = Decimal(str(sqrt(float(variance))))
        upper = mean + (stddev_multiplier * stdev)
        lower = mean - (stddev_multiplier * stdev)
        width_pct = ((upper - lower) / mean) * Decimal("100") if mean != 0 else Decimal("0")
        result.append(
            BollingerBandPoint(
                middle=mean,
                upper=upper,
                lower=lower,
                width_pct=width_pct,
            )
        )

    return result


def average_true_range(
    candles: Iterable[OHLCVCandle],
    period: int = 14,
) -> list[Decimal | None]:
    _validate_period(period)
    series = list(candles)
    if not series:
        return []

    true_ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in series:
        if previous_close is None:
            true_range = candle.high - candle.low
        else:
            true_range = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        true_ranges.append(true_range)
        previous_close = candle.close

    result: list[Decimal | None] = [None] * len(series)
    if len(true_ranges) < period:
        return result

    atr = sum(true_ranges[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = atr
    for index in range(period, len(true_ranges)):
        atr = ((atr * Decimal(period - 1)) + true_ranges[index]) / Decimal(period)
        result[index] = atr
    return result


def volume_profile(
    candles: Iterable[OHLCVCandle],
    *,
    bins: int = 12,
) -> list[VolumeProfileBin]:
    if bins <= 0:
        raise ValueError("bins must be positive.")
    series = list(candles)
    if not series:
        return []

    lowest = min(candle.low for candle in series)
    highest = max(candle.high for candle in series)
    if highest == lowest:
        total_volume = sum((candle.volume for candle in series), Decimal("0"))
        return [VolumeProfileBin(low=lowest, high=highest, volume=total_volume)]

    step = (highest - lowest) / Decimal(bins)
    profile = [
        VolumeProfileBin(
            low=lowest + (step * Decimal(index)),
            high=lowest + (step * Decimal(index + 1)),
            volume=Decimal("0"),
        )
        for index in range(bins)
    ]
    volumes = [Decimal("0") for _ in range(bins)]

    for candle in series:
        price = candle.typical_price
        index = int((price - lowest) / step)
        index = min(max(index, 0), bins - 1)
        volumes[index] += candle.volume

    return [
        VolumeProfileBin(low=item.low, high=item.high, volume=volume)
        for item, volume in zip(profile, volumes)
    ]


def latest_indicator_snapshot(
    series: CandleSeries,
    *,
    ema_fast_period: int = 9,
    ema_slow_period: int = 21,
    rsi_period: int = 14,
    macd_fast_period: int = 12,
    macd_slow_period: int = 26,
    macd_signal_period: int = 9,
    bollinger_period: int = 20,
    atr_period: int = 14,
    volume_profile_bins: int = 12,
) -> IndicatorSnapshot:
    candles = list(series)
    closes = series.closes()

    ema_fast = _last(exponential_moving_average(closes, ema_fast_period))
    ema_slow = _last(exponential_moving_average(closes, ema_slow_period))
    rsi = _last(relative_strength_index(closes, rsi_period))
    macd_point = _last(
        macd(
            closes,
            fast_period=macd_fast_period,
            slow_period=macd_slow_period,
            signal_period=macd_signal_period,
        )
    )
    bollinger = _last(bollinger_bands(closes, period=bollinger_period))
    atr = _last(average_true_range(candles, period=atr_period))
    profile = volume_profile(candles, bins=volume_profile_bins)

    return IndicatorSnapshot(
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi,
        macd=macd_point,
        bollinger=bollinger,
        atr=atr,
        volume_profile=profile,
    )


def _validate_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive.")


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_gain == 0 and avg_loss == 0:
        return Decimal("50")
    if avg_loss == 0:
        return Decimal("100")
    relative_strength = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))


def _last(values: list):
    for value in reversed(values):
        if value is not None:
            return value
    return None
