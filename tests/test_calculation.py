import unittest
from datetime import date

import pandas as pd

from procurement.calculate import Settings, _outlier_excess, _seasonal_factors, recommend
from procurement.ingest import SupplierData


def fixture(monthly_values=None, events=None, incoming=None, balance=0, pack=5):
    periods = pd.date_range('2024-01-01', '2026-08-01', freq='MS').date
    quantities = monthly_values or [30] * len(periods)
    monthly = pd.DataFrame({'code':'SKU-1', 'month':periods, 'qty':quantities})
    catalog = pd.DataFrame([{'code':'SKU-1','name':'Test product','article':'ABC',
                             'pack':pack,'category':'1','balance':balance,
                             'balance_source':'Test balance','pending':0}])
    event_columns = ['code','date','document','qty']
    incoming_columns = ['code','eta','qty']
    return SupplierData('IEK', monthly, pd.DataFrame(events or [], columns=event_columns),
                        catalog, pd.DataFrame(incoming or [], columns=incoming_columns),
                        date(2026, 9, 22), [])


class RecommendationTests(unittest.TestCase):
    def test_order_respects_pack_and_arrival_date(self):
        base = recommend(fixture(), Settings(30, 7, 0))
        arriving = recommend(fixture(incoming=[('SKU-1', date(2026, 10, 1), 12)]), Settings(30, 7, 0))
        late = recommend(fixture(incoming=[('SKU-1', date(2026, 12, 1), 12)]), Settings(30, 7, 0))
        self.assertGreater(int(base.iloc[0]['Рекомендовано']), int(arriving.iloc[0]['Рекомендовано']))
        self.assertEqual(int(base.iloc[0]['Рекомендовано']), int(late.iloc[0]['Рекомендовано']))
        self.assertEqual(int(arriving.iloc[0]['Рекомендовано']) % 5, 0)

    def test_stockout_increases_demand(self):
        data = fixture()
        stockouts = pd.DataFrame([{'code':'SKU-1','start':'2026-08-01','end':'2026-08-15'}])
        raw = recommend(data, Settings(30, 7, 0))
        corrected = recommend(data, Settings(30, 7, 0), stockouts)
        self.assertGreater(float(corrected.iloc[0]['Прогноз']), float(raw.iloc[0]['Прогноз']))
        self.assertGreater(float(corrected.iloc[0]['Упущенный спрос']), 0)

    def test_single_big_document_does_not_distort_regular_order(self):
        events = [('SKU-1', date(2026, 8, 1), 'A', 10),
                  ('SKU-1', date(2026, 8, 2), 'B', 10),
                  ('SKU-1', date(2026, 8, 3), 'C', 1000)]
        data = fixture(monthly_values=[30] * 31 + [1030], events=events)
        normal = recommend(fixture(), Settings(30, 7, 0))
        corrected = recommend(data, Settings(30, 7, 0))
        self.assertGreater(float(_outlier_excess(data.events).iloc[0]['excess']), 900)
        self.assertLess(int(corrected.iloc[0]['Рекомендовано']) - int(normal.iloc[0]['Рекомендовано']), 40)

    def test_seasonality_uses_month_pattern(self):
        qty = [120 if i % 12 in (8, 9, 10) else 20 for i in range(32)]
        seasonal = _seasonal_factors(fixture(monthly_values=qty).monthly)
        self.assertGreater(seasonal['SKU-1'][10], seasonal['SKU-1'][2] * 2)

    def test_unknown_balance_does_not_become_zero(self):
        self.assertTrue(recommend(fixture(balance=None), Settings()).empty)


if __name__ == '__main__':
    unittest.main()
