"""Transparent demand adjustments and date-aware replenishment calculation."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .ingest import SupplierData


MODEL_VERSION = 'regular-demand-v2'


@dataclass(frozen=True)
class Settings:
    lead_days: int = 30
    review_days: int = 7
    safety_days: int = 7
    external_growth: float = 0.0  # Decimal; 0.10 means +10%.

    def __post_init__(self):
        if not (1 <= self.lead_days <= 365 and 1 <= self.review_days <= 90
                and 0 <= self.safety_days <= 90 and -0.5 <= self.external_growth <= 2):
            raise ValueError('Некорректные параметры планирования')


def _month_start(day):
    return date(day.year, day.month, 1)


def _shift_month(day, count):
    month = day.year * 12 + day.month - 1 + count
    return date(month // 12, month % 12 + 1, 1)


def _outlier_excess(events: pd.DataFrame) -> pd.DataFrame:
    """Cap rare document-level spikes, retaining the ordinary part of a sale."""
    if events.empty:
        return pd.DataFrame(columns=['code', 'month', 'excess'])
    grouped = events.groupby(['code', 'date', 'document'], as_index=False)['qty'].sum()
    corrections = []
    for code, part in grouped.groupby('code'):
        values = part['qty']
        if len(values) < 3:
            continue
        median = float(values.median())
        mad = float((values - median).abs().median())
        threshold = max(10.0, median * 5, median + 6 * mad)
        flags = values > threshold
        if flags.sum() > max(1, math.floor(len(values) * 0.10)):
            continue  # Repeated large sales can be regular demand.
        for _, row in part.loc[flags].iterrows():
            corrections.append((code, _month_start(row['date']), float(row['qty'] - threshold)))
    if not corrections:
        return pd.DataFrame(columns=['code', 'month', 'excess'])
    return pd.DataFrame(corrections, columns=['code', 'month', 'excess']).groupby(
        ['code', 'month'], as_index=False)['excess'].sum()


def _stockout_adjustments(monthly: pd.DataFrame, stockouts: pd.DataFrame) -> pd.DataFrame:
    """Estimate lost units using nearby positive months and exact unavailable days."""
    if stockouts is None or stockouts.empty:
        return pd.DataFrame(columns=['code', 'month', 'lost'])
    results = []
    for _, row in stockouts.iterrows():
        code = str(row['code']).strip()
        start, end = row['start'], row['end']
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        if end < start or (end - start).days > 366:
            raise ValueError(f'Некорректный период stockout: {code}')
        history = monthly[monthly.code == code]
        if history.empty:
            continue
        current = _month_start(start)
        while current <= end:
            days = calendar.monthrange(current.year, current.month)[1]
            last = current + timedelta(days=days - 1)
            missing_days = (min(last, end) - max(current, start)).days + 1
            neighbors = history[(history.month >= _shift_month(current, -3)) &
                                (history.month <= _shift_month(current, 3)) &
                                (history.month != current) & (history.qty > 0)]
            rates = [float(r.qty) / calendar.monthrange(r.month.year, r.month.month)[1]
                     for r in neighbors.itertuples()]
            if rates:
                results.append((code, current, float(pd.Series(rates).median() * missing_days)))
            current = _shift_month(current, 1)
    if not results:
        return pd.DataFrame(columns=['code', 'month', 'lost'])
    return pd.DataFrame(results, columns=['code', 'month', 'lost']).groupby(
        ['code', 'month'], as_index=False)['lost'].sum()


def _seasonal_factors(monthly: pd.DataFrame) -> dict[str, dict[int, float]]:
    """Use the latest two complete historical years, with supplier shrinkage."""
    periods = monthly['month'].drop_duplicates()
    by_year = periods.groupby(periods.map(lambda d: d.year))
    years = sorted(year for year, values in by_year if {d.month for d in values} == set(range(1, 13)))[-2:]
    full = monthly[monthly.month.map(lambda d: d.year in years)].copy()
    if full.empty:
        return {}
    totals = full.groupby('month')['qty'].sum()
    supplier_factors = {}
    for month in range(1, 13):
        ratios = []
        for year in years:
            year_values = [float(totals.get(date(year, m, 1), 0)) for m in range(1, 13)]
            mean = sum(year_values) / 12
            if mean > 0:
                ratios.append(year_values[month - 1] / mean)
        supplier_factors[month] = max(0.3, min(3.0, sum(ratios) / len(ratios))) if ratios else 1.0
    answer = {}
    for code, group in full.groupby('code'):
        if (group.qty > 0).sum() < 12:
            answer[code] = supplier_factors
            continue
        ratios_by_month = {m: [] for m in range(1, 13)}
        for year, year_group in group.groupby(group.month.map(lambda d: d.year)):
            quantities = {r.month.month: float(r.qty) for r in year_group.itertuples()}
            mean = sum(quantities.get(m, 0) for m in range(1, 13)) / 12
            if mean > 0:
                for month in range(1, 13):
                    ratios_by_month[month].append(quantities.get(month, 0) / mean)
        raw = {m: (sum(v) / len(v) if v else supplier_factors[m]) for m, v in ratios_by_month.items()}
        # One noisy year must not dominate the forecast.
        factors = {m: max(0.2, min(3.0, 0.7 * raw[m] + 0.3 * supplier_factors[m])) for m in raw}
        normalization = sum(factors.values()) / 12
        answer[code] = {m: v / normalization for m, v in factors.items()}
    answer['__supplier__'] = supplier_factors
    return answer


def _forecast(code: str, monthly: pd.DataFrame, as_of: date, settings: Settings,
              factors: dict) -> tuple[float, float, float]:
    first = _month_start(as_of)
    history = monthly[monthly.month < first].sort_values('month')
    if history.empty or history.qty.sum() <= 0:
        return 0.0, 0.0, 0.0
    seasonal = factors.get(code, factors.get('__supplier__', {m: 1.0 for m in range(1, 13)}))
    recent = history.tail(6).copy()
    recent['adjusted'] = [float(r.qty) / max(0.2, seasonal[r.month.month]) for r in recent.itertuples()]
    weights = list(range(1, len(recent) + 1))
    base = sum(x * w for x, w in zip(recent.adjusted, weights)) / sum(weights)
    trend = 0.0
    if len(recent) >= 6 and (recent.qty > 0).sum() >= 4:
        older = recent.adjusted.iloc[:3].mean()
        newer = recent.adjusted.iloc[3:].mean()
        if older > 0 and newer > older * 1.05:
            trend = min(0.20, (newer / older - 1) * 0.25)
    end = as_of + timedelta(days=settings.lead_days + settings.review_days)
    demand = 0.0
    day = as_of
    while day < end:
        days_in_month = calendar.monthrange(day.year, day.month)[1]
        demand += base * seasonal[day.month] * (1 + trend) * (1 + settings.external_growth) / days_in_month
        day += timedelta(days=1)
    daily = demand / (settings.lead_days + settings.review_days)
    return demand, daily, trend


def recommend(data: SupplierData, settings: Settings, stockouts: pd.DataFrame | None = None,
              balances: pd.DataFrame | None = None, *, forecast_start: date | None = None) -> pd.DataFrame:
    """Recommend demand for a fixed [start, end) interval using only earlier data.

    Explicit forecast starts allow saved forecasts to begin after the last known
    operation. Omitting the argument retains the legacy calculation date.
    """
    start = forecast_start if forecast_start is not None else data.as_of
    # A later planning date cannot turn an old export's partial month into a
    # fully observed month. A last operation on month-end does close that month.
    known_until = min(start, data.as_of + timedelta(days=1))
    monthly = data.monthly[data.monthly.month < _month_start(known_until)].copy()
    events = data.events[data.events.date < known_until] if not data.events.empty else data.events
    excess = _outlier_excess(events)
    prior_stockouts = stockouts
    if stockouts is not None and not stockouts.empty:
        prior_stockouts = stockouts.copy()
        for field in ('start', 'end'):
            prior_stockouts[field] = pd.to_datetime(prior_stockouts[field], errors='raise').dt.date
        if (prior_stockouts.end < prior_stockouts.start).any():
            raise ValueError('Некорректный период stockout: конец раньше начала')
        prior_stockouts = prior_stockouts[prior_stockouts.start < known_until].copy()
        prior_stockouts['end'] = prior_stockouts.end.map(lambda day: min(day, known_until - timedelta(days=1)))
    lost = _stockout_adjustments(monthly, prior_stockouts)
    monthly = monthly.merge(excess, how='left', on=['code', 'month']).merge(lost, how='left', on=['code', 'month'])
    monthly['excess'] = pd.to_numeric(monthly['excess'], errors='coerce').fillna(0.0)
    monthly['lost'] = pd.to_numeric(monthly['lost'], errors='coerce').fillna(0.0)
    monthly['qty'] = (monthly.qty - monthly[['qty', 'excess']].min(axis=1) + monthly.lost).clip(lower=0)
    factors = _seasonal_factors(monthly)
    monthly_by_code = dict(tuple(monthly.groupby('code', sort=False)))
    incoming_by_code = dict(tuple(data.incoming.groupby('code', sort=False))) if not data.incoming.empty else {}
    balance_overrides = {}
    if balances is not None and not balances.empty:
        balance_overrides = dict(zip(balances.code.map(str), balances.balance))
    end = start + timedelta(days=settings.lead_days + settings.review_days)
    lines = []
    for product in data.catalog.itertuples(index=False):
        code = product.code
        balance = balance_overrides.get(code, product.balance)
        if balance is None or pd.isna(balance):
            continue
        item_history = monthly_by_code.get(code)
        if item_history is None:
            continue
        forecast, daily, trend = _forecast(code, item_history, start, settings, factors)
        incoming = incoming_by_code.get(code)
        on_time = 0.0
        if incoming is not None:
            if forecast_start is None:
                on_time = incoming[(incoming.eta > start) & (incoming.eta <= end)].qty.sum()
            else:
                on_time = incoming[(incoming.eta >= start) & (incoming.eta < end)].qty.sum()
        safety = daily * settings.safety_days
        raw = max(0, forecast + safety - float(balance) - float(on_time))
        pack = product.pack if product.pack is not None and not pd.isna(product.pack) and product.pack > 0 else 1
        recommended = int(math.ceil((raw - 1e-9) / pack) * pack) if raw > 0 else 0
        coverage_days = float(balance) / daily if daily > 0 else float('inf')
        risk = 'Высокая' if coverage_days < settings.lead_days else ('Средняя' if coverage_days < settings.lead_days + settings.review_days else 'Низкая')
        anomaly = float(item_history.excess.sum())
        compensated = float(item_history.lost.sum())
        source = 'Загруженный актуальный остаток' if code in balance_overrides else product.balance_source
        note = f'Прогноз {forecast:.1f} + резерв {safety:.1f} − остаток {float(balance):.1f} − прибудет {float(on_time):.1f}; кратность {int(pack)}.'
        if anomaly:
            note += f' Исключено разовых продаж: {anomaly:.0f}.'
        if compensated:
            note += f' Оценка спроса в stockout: +{compensated:.0f}.'
        if product.pack is None or pd.isna(product.pack):
            note += ' Кратность не подтверждена: принято 1.'
        lines.append(dict(Поставщик=data.name, **{'Код 1С':code, 'Артикул':product.article,
            'Наименование':product.name, 'Категория':product.category, 'Рекомендовано':recommended,
            'Срочность':risk, 'Прогноз':round(forecast, 1), 'Резерв':round(safety, 1),
            'Начало прогноза':start.isoformat(), 'Конец прогноза':end.isoformat(),
            'Остаток':round(float(balance), 1), 'В пути вовремя':round(float(on_time), 1),
            'В пути без даты':round(float(product.pending), 1), 'Кратность':int(pack),
            'Тренд':round(trend * 100, 1), 'Исключено выбросов':round(anomaly, 1),
            'Упущенный спрос':round(compensated, 1), 'Источник остатка':source, 'Обоснование':note}))
    result = pd.DataFrame(lines)
    if not result.empty:
        result = result.sort_values(['Поставщик', 'Рекомендовано', 'Код 1С'], ascending=[True, False, True])
    return result
