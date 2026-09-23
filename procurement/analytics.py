"""Compare immutable demand forecasts with explicitly complete sales periods."""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta
from itertools import accumulate

import pandas as pd

from .ingest import SupplierData


_MEASURES = ('Факт', 'Отклонение', 'Абсолютная ошибка', 'Ошибка, %')


def _day(value) -> date:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError('Не указана дата периода')
    return timestamp.date()


def _coverage_intervals(coverage: pd.DataFrame) -> dict[str, list[tuple[date, date]]]:
    """Convert inclusive declarations to a union of half-open intervals."""
    intervals = {}
    if coverage.empty:
        return intervals
    for supplier, group in coverage.groupby('supplier'):
        merged = []
        for start, end in sorted((_day(row.start), _day(row.end)) for row in group.itertuples()):
            if end < start:
                raise ValueError('Конец периода факта раньше начала')
            exclusive_end = end + timedelta(days=1)
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], exclusive_end))
            else:
                merged.append((start, exclusive_end))
        intervals[str(supplier)] = merged
    return intervals


def evaluate_forecast(rows: pd.DataFrame, actuals: pd.DataFrame,
                      coverage: pd.DataFrame, *, today: date | None = None) -> pd.DataFrame:
    """Score demand only when every day of a closed horizon is covered.

    Coverage end dates are inclusive; forecast end dates are exclusive. Missing
    SKU/day rows mean zero only inside confirmed supplier coverage. ``actuals``
    are canonical sales rows after replacement of overlapping uploads at import.
    Neither order edits nor later model calculations modify saved predictions.
    """
    result = rows.copy(deep=True)
    for field in _MEASURES:
        result[field] = float('nan')
    result['Статус'] = 'Ожидает факта'
    if result.empty:
        return result
    observed_today = today or date.today()
    intervals = _coverage_intervals(coverage)
    cumulative = {}
    if not actuals.empty:
        facts = actuals.copy()
        facts['date'] = facts['date'].map(_day)
        facts['supplier'] = facts['supplier'].astype(str)
        facts['code'] = facts['code'].astype(str)
        facts['qty'] = pd.to_numeric(facts['qty'], errors='raise')
        if not facts['qty'].map(lambda qty: pd.notna(qty) and 0 <= qty < float('inf')).all():
            raise ValueError('Фактические продажи должны быть конечными неотрицательными числами')
        daily = facts.groupby(['supplier', 'code', 'date'], as_index=False)['qty'].sum()
        for identity, group in daily.groupby(['supplier', 'code'], sort=False):
            group = group.sort_values('date')
            cumulative[identity] = (list(group['date']), [0.0, *accumulate(group['qty'])])
    status_cache = {}
    # Positional assignment also supports callers whose saved rows have repeated indices.
    for position, row in enumerate(result.to_dict('records')):
        supplier, code = str(row['Поставщик']), str(row['Код 1С'])
        start, end = _day(row['Начало прогноза']), _day(row['Конец прогноза'])
        if end <= start:
            raise ValueError('Конец прогноза должен быть позже начала')
        identity = (supplier, start, end)
        if identity not in status_cache:
            covered = sum(max(0, (min(end, right) - max(start, left)).days)
                          for left, right in intervals.get(supplier, []))
            status_cache[identity] = ('Ожидает факта' if end > observed_today or not covered else
                                      'Оценён' if covered == (end - start).days else 'Неполный период')
        status = status_cache[identity]
        result.iat[position, result.columns.get_loc('Статус')] = status
        if status != 'Оценён':
            continue
        days, sums = cumulative.get((supplier, code), ([], [0.0]))
        actual = float(sums[bisect_left(days, end)] - sums[bisect_left(days, start)])
        forecast = float(row['Прогноз'])
        if not 0 <= forecast < float('inf'):
            raise ValueError('Прогноз должен быть конечным неотрицательным числом')
        error = forecast - actual
        measures = (actual, error, abs(error), abs(error) / actual * 100 if actual else float('nan'))
        for field, value in zip(_MEASURES, measures):
            result.iat[position, result.columns.get_loc(field)] = value
    return result


def summary_metrics(evaluated: pd.DataFrame) -> dict:
    """Return unit errors and percentage WAPE/bias for fully evaluated rows."""
    scored = evaluated[evaluated['Статус'] == 'Оценён'] if 'Статус' in evaluated else pd.DataFrame()
    count = len(scored)
    if not count:
        return {'evaluated_count': 0, 'total_forecast': 0.0, 'total_actual': 0.0,
                'mae': None, 'wape': None, 'bias': None}
    forecast = pd.to_numeric(scored['Прогноз'], errors='raise')
    actual = pd.to_numeric(scored['Факт'], errors='raise')
    error = forecast - actual
    total_actual = float(actual.sum())
    return {'evaluated_count': count, 'total_forecast': float(forecast.sum()),
            'total_actual': total_actual, 'mae': float(error.abs().mean()),
            'wape': float(error.abs().sum() / total_actual * 100) if total_actual else None,
            'bias': float(error.sum() / total_actual * 100) if total_actual else None}


def sales_history(datasets: list[SupplierData]) -> pd.DataFrame:
    """Aggregate the latest complete snapshot per supplier, never summing uploads."""
    latest = {}
    for dataset in datasets:
        if dataset.name not in latest or dataset.as_of >= latest[dataset.name].as_of:
            latest[dataset.name] = dataset
    frames = []
    for dataset in latest.values():
        categories = dataset.catalog.drop_duplicates('code', keep='last').set_index('code')['category']
        frame = dataset.monthly[['month', 'code', 'qty']].copy()
        frame['supplier'] = dataset.name
        frame['category'] = frame['code'].map(categories).fillna('Не задана')
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=['month', 'supplier', 'category', 'qty'])
    return pd.concat(frames, ignore_index=True).groupby(
        ['month', 'supplier', 'category'], as_index=False)['qty'].sum().sort_values(['month', 'supplier', 'category'])
