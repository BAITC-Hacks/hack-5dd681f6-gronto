"""BI UI checks for report completeness, honest empty metrics and saved history."""

import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from bi_ui import parse_actuals_csv
from procurement.storage import Store
from test_calculation import fixture


class ActualReportParserTests(unittest.TestCase):
    def parse(self, body):
        return parse_actuals_csv(body.encode('utf-8-sig'), 'IEK', date(2024, 1, 1), date(2024, 1, 31))

    def test_preserves_codes_and_supports_declared_empty_report(self):
        result = self.parse('supplier;code;date;qty\nIEK;0001;2024-01-02;2,5\nIEK;NA;2024-01-03;1')
        self.assertEqual(result.code.tolist(), ['0001', 'NA'])
        self.assertEqual(result.qty.sum(), 3.5)
        self.assertTrue(self.parse('supplier;code;date;qty\n').empty)

    def test_rejects_infinite_negative_missing_and_non_numeric_quantities(self):
        for quantity in ['inf', '-inf', 'NaN', '-1', '', 'none', '1e999']:
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self.parse(f'supplier;code;date;qty\nIEK;1;2024-01-02;{quantity}')

    def test_rejects_mixed_suppliers_and_dates_outside_declared_window(self):
        invalid = ['Systeme Electric;1;2024-01-02;1', 'IEK;1;2024-02-01;1',
                   'IEK;1;02.01.2024;1', 'IEK;;2024-01-02;1', 'IEK;1;2024-02-31;1']
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.parse('supplier;code;date;qty\n' + row)


class BIWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'bi.sqlite3'
        environment = patch.dict(os.environ, {'PROCUREMENT_DB_PATH': str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(self.path)

    def app(self):
        return AppTest.from_string('from bi_ui import render_bi\n'
                                   'from procurement.storage import Store\n'
                                   'render_bi(Store())\n', default_timeout=20).run()

    def save_run(self, prospective=True):
        source = self.store.save_source('archive', 'IEK', 'IEK.zip', b'bi-fixture')
        dataset = self.store.save_dataset(source, fixture())
        rows = pd.DataFrame([{'Поставщик': 'IEK', 'Код 1С': 'SKU-1', 'Артикул': 'ABC',
                              'Наименование': 'Test product', 'Категория': '1', 'Прогноз': 10.0,
                              'Рекомендовано': 1000, 'Начало прогноза': '2024-01-01',
                              'Конец прогноза': '2024-02-01'}])
        return self.store.save_run({'rows': rows, 'issues': [], 'dates': []}, {
            'title': 'Тестовый прогноз', 'prospective': prospective, 'model_version': 'test',
            'dataset_ids': [dataset], 'sources': {'IEK': source}, 'params': {'review': 7}})

    def test_empty_workspace_renders_without_claiming_accuracy(self):
        app = self.app()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.tabs), 4)
        self.assertEqual(len(app.metric), 0)

    def test_absent_then_complete_fact_updates_real_metrics(self):
        self.save_run()
        app = self.app()
        self.assertEqual(len(app.exception), 0)
        metrics = {metric.label: metric.value for metric in app.metric}
        self.assertEqual(metrics['Оценено позиций'], '0 / 1')
        self.assertEqual(metrics['WAPE'], '—')
        actual = pd.DataFrame([{'code': 'SKU-1', 'date': date(2024, 1, 10), 'qty': 8.0}])
        self.store.import_actuals(actual, 'IEK', date(2024, 1, 1), date(2024, 1, 31))
        app.run()
        self.assertEqual(len(app.exception), 0)
        metrics = {metric.label: metric.value for metric in app.metric}
        self.assertEqual(metrics['Оценено позиций'], '1 / 1')
        self.assertEqual(metrics['MAE, шт.'], '2,0')
        self.assertEqual(metrics['WAPE'], '25,0 %')
        self.assertEqual(metrics['Смещение'], '+25,0 %')

    def test_historical_runs_excluded_until_requested(self):
        self.save_run(prospective=False)
        app = self.app()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(any(metric.label == 'WAPE' for metric in app.metric))
        app.toggle(key='bi_include_historical').set_value(True).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('Исторический сценарий' in item.value for item in app.warning))
        self.assertTrue(any(metric.label == 'WAPE' for metric in app.metric))

    def test_report_cannot_be_saved_without_completeness_confirmation(self):
        uploaded = io.BytesIO(b'supplier;code;date;qty\n')
        uploaded.name = 'zero_sales.csv'
        with patch('streamlit.file_uploader', return_value=uploaded):
            app = self.app()
            self.assertEqual(len(app.exception), 0)
            app.button[0].click().run()
            self.assertTrue(self.store.list_actual_imports().empty)
            self.assertTrue(any('Подтвердите полноту' in item.value for item in app.error))
            app.checkbox[0].check().run()
            app.button[0].click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(self.store.list_actual_imports()), 1)
            self.assertFalse(self.store.get_coverage().empty)
            self.assertTrue(any('сохранён' in item.value for item in app.success))


if __name__ == '__main__':
    unittest.main()
