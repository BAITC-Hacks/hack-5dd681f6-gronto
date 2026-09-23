import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal

from procurement.ingest import SupplierData
from procurement.storage import Store


def sample_data(supplier='IEK'):
    return SupplierData(
        supplier,
        pd.DataFrame([['001', date(2026, 8, 1), 10.0]], columns=['code', 'month', 'qty']),
        pd.DataFrame([['001', date(2026, 9, 1), 'doc-1', 2.0]], columns=['code', 'date', 'document', 'qty']),
        pd.DataFrame([['001', 'Product', 5, None]], columns=['code', 'name', 'pack', 'balance']),
        pd.DataFrame(columns=['code', 'eta', 'qty']),
        date(2026, 9, 1), ['An issue'],
    )


def actuals(*rows):
    return pd.DataFrame(rows, columns=['code', 'date', 'qty'])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'db.sqlite3'
        self.store = Store(self.path)

    def test_sources_are_scoped_deduplicated_and_persist(self):
        first = self.store.save_source('archive', 'IEK', 'original.zip', b'archive-content')
        self.assertEqual(first, self.store.save_source('archive', 'IEK', 'renamed.zip', b'archive-content'))
        other = self.store.save_source('archive', 'Systeme Electric', 'original.zip', b'archive-content')
        other_kind = self.store.save_source('balances', 'IEK', 'original.zip', b'archive-content')
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, other_kind)
        reopened = Store(self.path)
        self.assertEqual(reopened.get_source(first)['content'], b'archive-content')
        self.assertEqual(reopened.get_source(first)['filename'], 'original.zip')
        self.assertEqual(len(reopened.list_sources(kind='archive', supplier='IEK')), 1)
        self.assertEqual(len(reopened.list_sources()), 3)

    def test_dataset_immutable_deduplicated_and_dates_restored(self):
        source_id = self.store.save_source('archive', 'IEK', 'archive.zip', b'content')
        data = sample_data()
        dataset_id = self.store.save_dataset(source_id, data)
        self.assertEqual(dataset_id, self.store.save_dataset(source_id, data))
        data.monthly.loc[0, 'qty'] = 999
        self.assertNotEqual(dataset_id, self.store.save_dataset(source_id, data))
        restored = Store(self.path).get_dataset(dataset_id)
        expected = sample_data()
        for name in ['monthly', 'events', 'catalog', 'incoming']:
            assert_frame_equal(getattr(restored, name), getattr(expected, name))
        self.assertEqual(restored.as_of, date(2026, 9, 1))
        self.assertEqual(restored.issues, ['An issue'])
        self.assertEqual(len(self.store.list_datasets()), 2)
        with self.assertRaises(ValueError):
            self.store.save_dataset(source_id, sample_data('Systeme Electric'))

    def test_forecast_and_metadata_immutable_edits_separate_by_supplier(self):
        rows = pd.DataFrame([
            {'Поставщик': 'IEK', 'Код 1С': '001', 'Рекомендовано': 10, 'Прогноз': 6.5},
            {'Поставщик': 'Systeme Electric', 'Код 1С': '001', 'Рекомендовано': 20, 'Прогноз': 12.5},
        ])
        metadata = {'title': 'September', 'params': {'days': 37}, 'sources': ['a'],
                    'forecast_date': date(2026, 9, 1), 'model_version': 'v1', '__type__': 'user-value'}
        result = {'rows': rows, 'issues': ['warning'], 'dates': [date(2026, 9, 1)]}
        run_id = self.store.save_run(result, metadata)
        rows.loc[0, 'Прогноз'] = 1000
        metadata['params']['days'] = 500
        self.store.save_order_quantity(run_id, 'IEK', '001', 13)
        self.store.save_order_quantity(run_id, 'Systeme Electric', '001', 4)
        self.store.save_order_quantity(run_id, 'IEK', '001', 14)
        saved = Store(self.path).get_run(run_id)
        self.assertEqual(saved['result']['rows'].iloc[0]['Прогноз'], 6.5)
        self.assertEqual(saved['result']['rows'].iloc[0]['Рекомендовано'], 10)
        self.assertEqual(saved['metadata']['params']['days'], 37)
        self.assertEqual(saved['metadata']['__type__'], 'user-value')
        self.assertEqual(saved['result']['dates'], [date(2026, 9, 1)])
        self.assertEqual(saved['edits'].set_index('supplier')['quantity'].to_dict(), {'IEK': 14, 'Systeme Electric': 4})
        self.assertEqual(self.store.list_runs().iloc[0]['title'], 'September')
        for quantity in [-1, 1.5, float('inf'), float('nan'), None, True, 'bad', 2**63]:
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self.store.save_order_quantity(run_id, 'IEK', '001', quantity)
        with self.assertRaises(ValueError):
            self.store.save_order_quantity(run_id, 'IEK', 'unknown', 1)
        self.assertEqual(len(self.store.get_run(run_id)['edits']), 2)

    def test_actual_overlap_replaces_whole_interval_and_preserves_history(self):
        initial = actuals(('001', '2026-09-01', 2), ('001', '2026-09-01', 3),
                          ('002', '2026-09-02', 10), ('001', '2026-09-05', 8))
        first = self.store.import_actuals(initial, 'IEK', '2026-09-01', '2026-09-05', 'first.csv', b'raw-first')
        self.assertEqual(self.store.get_actuals().iloc[0]['qty'], 5)
        self.store.import_actuals(actuals(('001', '2026-09-02', 50)), 'Systeme Electric', '2026-09-01', '2026-09-05')
        correction = actuals(('003', '2026-09-03', 7))
        second = self.store.import_actuals(correction, 'IEK', '2026-09-02', '2026-09-04', 'correction.csv', b'raw-correction')
        self.store.import_actuals(correction, 'IEK', '2026-09-02', '2026-09-04', 'again.csv', b'raw-correction')
        current = Store(self.path).get_actuals('IEK')
        self.assertEqual(current['code'].tolist(), ['001', '003', '001'])
        self.assertEqual(current['qty'].tolist(), [5, 7, 8])
        self.assertEqual(current['date'].tolist(), [date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 5)])
        self.assertEqual(self.store.get_actuals('Systeme Electric')['qty'].sum(), 50)
        self.assertEqual(len(self.store.get_actuals(start='2026-09-03', end='2026-09-04')), 1)
        self.assertEqual(len(self.store.get_coverage('IEK')), 3)
        self.assertEqual(len(self.store.list_sources(kind='actuals', supplier='IEK')), 2)
        history = self.store.get_actual_import(first)
        self.assertEqual(history['rows']['qty'].sum(), 23)
        self.assertEqual(self.store.get_source(history['source_id'])['content'], b'raw-first')
        self.assertEqual(self.store.get_actual_import(second)['filename'], 'correction.csv')

    def test_empty_report_declares_zeros_and_keeps_coverage(self):
        self.store.import_actuals(actuals(('001', '2026-09-02', 10)), 'IEK', '2026-09-01', '2026-09-05')
        import_id = self.store.import_actuals(actuals(), 'IEK', '2026-09-01', '2026-09-05')
        self.assertTrue(Store(self.path).get_actuals().empty)
        coverage = self.store.get_coverage()
        self.assertEqual(len(coverage), 2)
        self.assertEqual(coverage.iloc[-1]['import_id'], import_id)
        self.assertEqual(coverage.iloc[-1]['start'], date(2026, 9, 1))
        self.assertEqual(self.store.list_actual_imports().iloc[0]['row_count'], 0)

    def test_invalid_actuals_leave_all_tables_unchanged(self):
        self.store.import_actuals(actuals(('001', '2026-09-02', 10)), 'IEK', '2026-09-01', '2026-09-05', content=b'valid')
        invalid = [
            actuals((None, '2026-09-02', 4)), actuals((pd.NA, '2026-09-02', 4)),
            actuals((' ', '2026-09-02', 4)), actuals(('001', None, 4)),
            actuals(('001', pd.NaT, 4)), actuals(('001', 'bad-date', 4)),
            actuals(('001', '2026-08-31', 4)), actuals(('001', '2026-09-06', 4)),
            actuals(('001', '2026-09-02', -1)), actuals(('001', '2026-09-02', float('nan'))),
            actuals(('001', '2026-09-02', float('inf'))), actuals(('001', '2026-09-02', None)),
            actuals(('001', '2026-09-02', True)), pd.DataFrame({'code': ['001']}),
        ]
        for frame in invalid:
            with self.subTest(frame=frame.to_dict()), self.assertRaises(ValueError):
                self.store.import_actuals(frame, 'IEK', '2026-09-01', '2026-09-05', content=b'invalid')
        self.assertEqual(len(self.store.list_actual_imports()), 1)
        self.assertEqual(len(self.store.list_sources()), 1)
        self.assertEqual(self.store.get_actuals()['qty'].tolist(), [10])

    def test_database_failure_rolls_back_source_import_and_replacement(self):
        self.store.import_actuals(actuals(('001', '2026-09-02', 10)), 'IEK', '2026-09-01', '2026-09-05', content=b'valid')
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('''CREATE TRIGGER simulate_disk_error BEFORE INSERT ON actuals
                                  WHEN NEW.code='002' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.import_actuals(actuals(('002', '2026-09-02', 4)), 'IEK', '2026-09-01', '2026-09-05', content=b'correction')
        self.assertEqual(len(self.store.list_actual_imports()), 1)
        self.assertEqual(len(self.store.list_sources()), 1)
        self.assertEqual(self.store.get_actuals()['code'].tolist(), ['001'])
        self.assertEqual(self.store.get_actuals()['qty'].tolist(), [10])

    def test_preferences_and_environment_path(self):
        self.assertEqual(self.store.get_preferences(), {})
        value = {'sources': {'IEK': 'hash'}, 'lead_days': 45}
        self.store.save_preferences(value)
        value['lead_days'] = 10
        with patch.dict(os.environ, {'PROCUREMENT_DB_PATH': str(self.path)}):
            self.assertEqual(Store().get_preferences()['lead_days'], 45)
        self.store.save_preferences({'lead_days': 60})
        self.assertEqual(Store(self.path).get_preferences(), {'lead_days': 60})

    def test_newer_schema_is_never_downgraded(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):
            Store(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_special_dataframe_values_roundtrip(self):
        frame = pd.DataFrame({'code': pd.Series(['001', pd.NA], dtype='string'),
                              'qty': pd.Series([3, pd.NA], dtype='Int64'),
                              'when': pd.to_datetime(['2026-09-01', None]),
                              'ratio': [float('inf'), float('nan')]})
        run_id = self.store.save_run({'rows': frame, 'dates': [datetime(2026, 9, 1)]}, {})
        assert_frame_equal(self.store.get_run(run_id)['result']['rows'], frame)


if __name__ == '__main__':
    unittest.main()
