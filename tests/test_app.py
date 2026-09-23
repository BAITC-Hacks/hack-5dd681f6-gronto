"""Regression checks for the order workspace across Streamlit reruns."""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from procurement.storage import Store
from test_calculation import fixture


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'workspace.sqlite3'
        environment = patch.dict(os.environ, {'PROCUREMENT_DB_PATH': str(self.database)})
        environment.start()
        self.addCleanup(environment.stop)
        self.uploads = patch('streamlit.file_uploader', side_effect=lambda label, **kwargs:
                             io.BytesIO(b'workspace-test') if label == 'IEK' else None)
        self.loader = patch('procurement.ingest.load_archive', return_value=fixture())
        self.uploads.start()
        self.loader.start()
        self.addCleanup(self.uploads.stop)
        self.addCleanup(self.loader.stop)
        self.app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'),
                                    default_timeout=20).run()
        self.app.button[0].click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.dataframe), 1)

    def reopen(self):
        self.uploads.stop()
        with patch('streamlit.file_uploader', return_value=None):
            return AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'),
                                     default_timeout=20).run()

    def test_filter_and_search_preserve_calculation(self):
        self.app.selectbox[0].select('IEK').run()
        self.assertEqual(len(self.app.dataframe), 1)
        self.assertEqual(len(self.app.get('download_button')), 1)
        self.app.text_input[0].set_value('missing-product').run()
        self.assertEqual(len(self.app.dataframe), 0)
        self.assertEqual(len(self.app.get('download_button')), 0)
        self.app.text_input[0].set_value('ABC').run()
        self.assertEqual(len(self.app.dataframe), 1)
        self.assertEqual(self.app.dataframe[0].value.iloc[0]['Артикул'], 'ABC')
        self.app.toggle[0].set_value(False).run()
        self.assertEqual(len(self.app.get('download_button')), 1)
        self.assertEqual(len(self.app.exception), 0)

    def test_changed_parameters_require_fresh_calculation(self):
        original = self.app.dataframe[0].value.iloc[0]['Рекомендовано']
        self.app.number_input[0].set_value(60).run()
        self.assertEqual(len(self.app.get('download_button')), 0)
        self.assertTrue(any('параметры изменились' in item.value for item in self.app.info))
        self.app.button[0].click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertGreater(self.app.dataframe[0].value.iloc[0]['Рекомендовано'], original)
        self.assertEqual(len(self.app.get('download_button')), 1)

    def test_saved_quantities_survive_empty_search(self):
        self.app.session_state.order_quantities[('IEK', 'SKU-1')] = 50
        self.app.text_input[0].set_value('missing-product').run()
        self.assertEqual(len(self.app.dataframe), 0)
        self.app.text_input[0].set_value('').run()
        self.assertEqual(self.app.dataframe[0].value.iloc[0]['Рекомендовано'], 50)
        self.assertEqual(len(self.app.exception), 0)

    def test_new_browser_session_restores_files_settings_forecast_and_edits(self):
        self.app.number_input(key='lead_iek').set_value(60).run()
        self.app.button[0].click().run()
        store = Store(self.database)
        run_id = self.app.session_state.order_run_id
        original = store.get_run(run_id)['result']['rows'].copy()
        store.save_order_quantity(run_id, 'IEK', 'SKU-1', 50)
        reopened = self.reopen()
        self.assertEqual(len(reopened.exception), 0)
        self.assertEqual(reopened.number_input(key='lead_iek').value, 60)
        self.assertEqual(reopened.session_state.order_run_id, run_id)
        self.assertEqual(reopened.dataframe[0].value.iloc[0]['Рекомендовано'], 50)
        self.assertEqual(len(reopened.get('download_button')), 1)
        self.assertEqual(len(store.list_runs()), 2)
        self.assertTrue(store.get_run(run_id)['result']['rows'].equals(original))

    def test_open_previous_run_from_bi_restores_its_parameters(self):
        first_id = self.app.session_state.order_run_id
        self.app.number_input(key='lead_iek').set_value(60).run()
        self.app.button[0].click().run()
        self.assertNotEqual(self.app.session_state.order_run_id, first_id)
        self.app.radio(key='workspace_page').set_value('BI-аналитика').run()
        self.assertEqual(len(self.app.exception), 0)
        self.app.selectbox(key='bi_history_run').select(first_id).run()
        self.app.button(key='bi_open_run').click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.radio(key='workspace_page').value, 'План закупок')
        self.assertEqual(self.app.number_input(key='lead_iek').value, 30)
        self.assertEqual(self.app.session_state.order_run_id, first_id)
        self.assertEqual(len(self.app.get('download_button')), 1)

    def test_only_orders_filter_and_metrics_use_saved_manual_quantities(self):
        store = Store(self.database)
        saved = store.get_run(self.app.session_state.order_run_id)
        saved['result']['rows']['Рекомендовано'] = 0
        run_id = store.save_run(saved['result'], saved['metadata'])
        store.save_order_quantity(run_id, 'IEK', 'SKU-1', 50)
        preferences = store.get_preferences()
        preferences['last_run_id'] = run_id
        store.save_preferences(preferences)
        reopened = self.reopen()
        self.assertEqual(len(reopened.exception), 0)
        self.assertEqual(reopened.dataframe[0].value.iloc[0]['Рекомендовано'], 50)
        self.assertEqual({item.label: item.value for item in reopened.metric}['Позиций к заказу'], '1')
        self.assertEqual(store.get_run(run_id)['result']['rows'].iloc[0]['Рекомендовано'], 0)

    def test_optional_multi_supplier_balance_file_is_saved(self):
        self.uploads.stop()
        with patch('streamlit.file_uploader', side_effect=lambda label, **kwargs:
                   io.BytesIO(b'supplier,code,balance\nIEK,SKU-1,10\n')
                   if label == 'Актуальные остатки' else None):
            self.app.run()
            self.app.button[0].click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.error), 0)
        self.assertEqual(len(Store(self.database).list_sources(kind='balances')), 1)
        self.assertEqual(self.app.dataframe[0].value.iloc[0]['Остаток'], 10)


if __name__ == '__main__':
    unittest.main()
