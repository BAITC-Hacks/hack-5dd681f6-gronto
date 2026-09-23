import glob
import unittest
from pathlib import Path

from procurement.ingest import load_archive


class ArchiveTests(unittest.TestCase):
    def test_bad_archive_is_reported(self):
        with self.assertRaises(Exception):
            load_archive(b'not a zip', 'IEK')

    @unittest.skipUnless(glob.glob('../upload/*.zip'), 'Optional local mockup archives unavailable')
    def test_supplied_mockups_have_joinable_products(self):
        for supplier, name in [('IEK', 'IEK.zip'), ('Systeme Electric', 'Systeme electric.zip')]:
            with self.subTest(supplier=supplier):
                data = load_archive((Path('../upload') / name).read_bytes(), supplier)
                self.assertGreater(len(data.monthly), 1000)
                self.assertGreater(len(data.events), 1000)
                self.assertGreater(len(data.catalog), 100)
                self.assertGreater(sum(data.catalog.balance.notna()), 100)


if __name__ == '__main__':
    unittest.main()
