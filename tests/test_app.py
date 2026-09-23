"""Regression checks for the order workspace across Streamlit reruns."""

import io
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from test_calculation import fixture


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == '__main__':
    unittest.main()
