"""Date coverage, fixed forecast snapshots, and forecast-quality regressions."""

from datetime import date, timedelta
import unittest

import pandas as pd

from procurement.analytics import evaluate_forecast, sales_history, summary_metrics
from procurement.calculate import MODEL_VERSION, Settings, _seasonal_factors, recommend
from test_calculation import fixture


def forecast_rows(forecast=20.0, supplier='IEK', code='001', start='2026-09-01', end='2026-09-04'):
    return pd.DataFrame([{'Поставщик': supplier, 'Код 1С': code, 'Прогноз': forecast,
                          'Рекомендовано': 999, 'Начало прогноза': start, 'Конец прогноза': end}])


def facts(*rows):
    return pd.DataFrame(rows, columns=['supplier', 'code', 'date', 'qty'])


def coverage(*rows):
    return pd.DataFrame(rows, columns=['supplier', 'start', 'end'])


TODAY = date(2026, 9, 5)
FULL = coverage(('IEK', '2026-09-01', '2026-09-03'))


class ForecastEvaluationTests(unittest.TestCase):
    def test_missing_coverage_never_means_zero_sales(self):
        result = evaluate_forecast(forecast_rows(), facts(), coverage(), today=TODAY)
        self.assertEqual(result.iloc[0]['Статус'], 'Ожидает факта')
        self.assertTrue(pd.isna(result.iloc[0]['Факт']))
        self.assertIsNone(summary_metrics(result)['wape'])

    def test_explicitly_covered_empty_sales_are_zero(self):
        result = evaluate_forecast(forecast_rows(), facts(), FULL, today=TODAY)
        row = result.iloc[0]
        self.assertEqual(row['Статус'], 'Оценён')
        self.assertEqual(row['Факт'], 0)
        self.assertEqual(row['Абсолютная ошибка'], 20)
        self.assertTrue(pd.isna(row['Ошибка, %']))
        metrics = summary_metrics(result)
        self.assertEqual(metrics['mae'], 20)
        self.assertIsNone(metrics['wape'])
        self.assertIsNone(metrics['bias'])

    def test_partial_coverage_is_not_scored(self):
        result = evaluate_forecast(forecast_rows(), facts(('IEK', '001', '2026-09-01', 4)),
                                   coverage(('IEK', '2026-09-01', '2026-09-02')), today=TODAY)
        self.assertEqual(result.iloc[0]['Статус'], 'Неполный период')
        self.assertTrue(pd.isna(result.iloc[0]['Факт']))
        self.assertEqual(summary_metrics(result)['evaluated_count'], 0)

    def test_overlapping_coverage_is_unioned_and_boundary_days_are_exact(self):
        actuals = facts(('IEK', '001', '2026-08-31', 999), ('IEK', '001', '2026-09-01', 5),
                        ('IEK', '001', '2026-09-03', 7), ('IEK', '001', '2026-09-04', 999))
        periods = coverage(('IEK', '2026-09-01', '2026-09-02'),
                           ('IEK', '2026-09-02', '2026-09-03'), ('IEK', '2026-09-01', '2026-09-03'))
        result = evaluate_forecast(forecast_rows(), actuals, periods, today=TODAY)
        self.assertEqual(result.iloc[0]['Статус'], 'Оценён')
        self.assertEqual(result.iloc[0]['Факт'], 12)
        self.assertEqual(result.iloc[0]['Отклонение'], 8)

    def test_gap_in_coverage_stays_partial(self):
        periods = coverage(('IEK', '2026-09-01', '2026-09-01'), ('IEK', '2026-09-03', '2026-09-03'))
        result = evaluate_forecast(forecast_rows(), facts(), periods, today=TODAY)
        self.assertEqual(result.iloc[0]['Статус'], 'Неполный период')

    def test_future_horizon_is_not_scored_even_with_future_coverage(self):
        result = evaluate_forecast(forecast_rows(end='2026-09-06'), facts(),
                                   coverage(('IEK', '2026-09-01', '2026-09-30')), today=TODAY)
        self.assertEqual(result.iloc[0]['Статус'], 'Ожидает факта')
        self.assertTrue(pd.isna(result.iloc[0]['Факт']))
        closed_today = evaluate_forecast(forecast_rows(end=TODAY.isoformat()), facts(),
                                         coverage(('IEK', '2026-09-01', '2026-09-04')), today=TODAY)
        self.assertEqual(closed_today.iloc[0]['Статус'], 'Оценён')

    def test_supplier_identity_and_string_codes_remain_distinct(self):
        rows = pd.concat([forecast_rows(20), forecast_rows(30, supplier='Systeme Electric')])
        actuals = facts(('IEK', '001', '2026-09-02', 5), ('IEK', '1', '2026-09-02', 500),
                        ('Systeme Electric', '001', '2026-09-02', 10))
        periods = pd.concat([FULL, coverage(('Systeme Electric', '2026-09-01', '2026-09-03'))])
        result = evaluate_forecast(rows, actuals, periods, today=TODAY)
        self.assertEqual(result['Факт'].tolist(), [5.0, 10.0])
        metrics = summary_metrics(result)
        self.assertEqual(metrics['evaluated_count'], 2)
        self.assertEqual(metrics['total_forecast'], 50)
        self.assertEqual(metrics['total_actual'], 15)
        self.assertAlmostEqual(metrics['wape'], 35 / 15 * 100)
        self.assertAlmostEqual(metrics['bias'], 35 / 15 * 100)
        self.assertEqual(metrics['mae'], 17.5)
        self.assertGreater(metrics['wape'], 100)

    def test_original_rows_and_order_edits_do_not_change_forecast_quality(self):
        rows = forecast_rows()
        original = rows.copy(deep=True)
        actuals = facts(('IEK', '001', '2026-09-02', 30))
        result = evaluate_forecast(rows, actuals, FULL, today=TODAY)
        pd.testing.assert_frame_equal(rows, original)
        rows['Рекомендовано'] = 0
        edited = evaluate_forecast(rows, actuals, FULL, today=TODAY)
        pd.testing.assert_frame_equal(result.drop(columns='Рекомендовано'), edited.drop(columns='Рекомендовано'))
        self.assertEqual(result.iloc[0]['Отклонение'], -10)
        self.assertAlmostEqual(summary_metrics(result)['bias'], -100 / 3)
        self.assertGreater(result.iloc[0]['Ошибка, %'], 0)

    def test_calendar_year_boundary(self):
        rows = forecast_rows(start='2026-12-31', end='2027-01-02')
        result = evaluate_forecast(rows, facts(('IEK', '001', '2027-01-01', 4)),
                                   coverage(('IEK', '2026-12-31', '2026-12-31'),
                                            ('IEK', '2027-01-01', '2027-01-01')), today=date(2027, 1, 2))
        self.assertEqual(result.iloc[0]['Статус'], 'Оценён')
        self.assertEqual(result.iloc[0]['Факт'], 4)

    def test_empty_forecasts_keep_evaluation_schema(self):
        result = evaluate_forecast(pd.DataFrame(), facts(), coverage(), today=TODAY)
        self.assertTrue(result.empty)
        self.assertIn('Статус', result)
        self.assertEqual(summary_metrics(result)['evaluated_count'], 0)


class ForecastDateTests(unittest.TestCase):
    def test_explicit_origin_is_saved_with_exclusive_horizon(self):
        data = fixture()
        start = data.as_of + timedelta(days=1)
        rows = recommend(data, Settings(30, 7, 0), forecast_start=start)
        self.assertEqual(rows.iloc[0]['Начало прогноза'], '2026-09-23')
        self.assertEqual(rows.iloc[0]['Конец прогноза'], '2026-10-30')
        self.assertEqual(data.as_of, date(2026, 9, 22))
        self.assertEqual(MODEL_VERSION, 'regular-demand-v2')

    def test_future_months_events_and_stockouts_cannot_change_past_forecast(self):
        start = date(2025, 9, 1)
        known_events = [('SKU-1', date(2025, 8, 1), 'A', 10),
                        ('SKU-1', date(2025, 8, 2), 'B', 10),
                        ('SKU-1', date(2025, 8, 3), 'C', 1000)]
        data = fixture(events=known_events)
        baseline = recommend(data, Settings(), pd.DataFrame([
            {'code': 'SKU-1', 'start': '2025-08-20', 'end': '2025-08-31'}]), forecast_start=start)
        changed = fixture(events=known_events + [
            ('SKU-1', date(2025, 9, 1), 'FUTURE-A', 4000),
            ('SKU-1', date(2025, 10, 1), 'FUTURE-B', 5000)])
        changed.monthly.loc[changed.monthly.month >= start, 'qty'] = 1000000
        with_future = recommend(changed, Settings(), pd.DataFrame([
            {'code': 'SKU-1', 'start': '2025-08-20', 'end': '2025-10-01'},
            {'code': 'SKU-1', 'start': '2025-09-01', 'end': '2025-10-15'}]), forecast_start=start)
        pd.testing.assert_frame_equal(baseline, with_future)

    def test_seasonality_uses_recent_complete_years_after_2025(self):
        periods = pd.date_range('2027-01-01', '2029-08-01', freq='MS').date
        monthly = pd.DataFrame({'code': 'SKU-1', 'month': periods,
                                'qty': [120 if day.month == 10 else 20 for day in periods]})
        factors = _seasonal_factors(monthly)
        self.assertGreater(factors['SKU-1'][10], factors['SKU-1'][2] * 2)
        partial = monthly[monthly.month.map(lambda day: day.year == 2029)]
        self.assertEqual(_seasonal_factors(partial), {})

    def test_deliveries_respect_explicit_half_open_horizon(self):
        start = date(2026, 9, 23)
        end = start + timedelta(days=37)
        rows = recommend(fixture(incoming=[('SKU-1', start, 7), ('SKU-1', end, 1000)]),
                         Settings(30, 7, 0), forecast_start=start)
        self.assertEqual(rows.iloc[0]['В пути вовремя'], 7)

    def test_later_start_does_not_complete_partial_source_month(self):
        known_events = [('SKU-1', date(2026, 8, 1), 'A', 10),
                        ('SKU-1', date(2026, 8, 2), 'B', 10),
                        ('SKU-1', date(2026, 8, 3), 'C', 1000)]
        original = fixture(events=known_events)
        changed = fixture(events=known_events + [
            ('SKU-1', date(2026, 9, 23), 'AFTER-EXPORT-A', 4000),
            ('SKU-1', date(2026, 10, 1), 'AFTER-EXPORT-B', 5000)])
        changed.monthly = pd.concat([changed.monthly, pd.DataFrame([
            {'code': 'SKU-1', 'month': date(2026, 9, 1), 'qty': 1000000},
            {'code': 'SKU-1', 'month': date(2026, 10, 1), 'qty': 1000000}])], ignore_index=True)
        start = date(2026, 11, 1)
        pd.testing.assert_frame_equal(recommend(original, Settings(), forecast_start=start),
                                      recommend(changed, Settings(), forecast_start=start))

    def test_sales_history_does_not_sum_repeated_supplier_snapshots(self):
        old = fixture()
        updated = fixture(monthly_values=[40] * len(old.monthly))
        updated.as_of += timedelta(days=1)
        other = fixture()
        other.name = 'Systeme Electric'
        history = sales_history([updated, old, other])
        self.assertEqual(history[history.supplier == 'IEK']['qty'].sum(), 40 * len(old.monthly))
        self.assertEqual(history[history.supplier == 'Systeme Electric']['qty'].sum(), 30 * len(old.monthly))


if __name__ == '__main__':
    unittest.main()
