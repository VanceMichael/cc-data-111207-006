import json
import unittest
from pathlib import Path
from reader import inspect
from lab import serving, get_json
from source import Source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parents[1] / "fixtures/source.json").read_text(encoding="utf-8"))
        self.source = Source(self.fixture)

    def test_first_page_is_not_full(self):
        with serving(Source(self.fixture)) as base:
            result=inspect(base,self.fixture)
        self.assertEqual(len(result['门店']),1)
        self.assertEqual(result['后续链接'],'/page?token=opaque-x')

    def test_expiry_is_distinct_from_empty(self):
        source=Source(self.fixture)
        self.assertEqual(source.handle(self.fixture['expired'],{})[0],410)
        self.assertEqual(source.handle('/delta?token=delta-2',{})[0],200)

    def test_null_and_absent_survive_transport(self):
        with serving(Source(self.fixture)) as base:
            page=get_json(base+'/delta?token=delta-1')
        self.assertIsNone(page['value'][0]['phone'])
        self.assertNotIn('name',page['value'][0])

    def test_page_failure(self):
        source=Source(self.fixture)
        source.fail_path='/page?token=opaque-x'
        self.assertEqual(source.handle(source.fail_path,{})[0],503)


if __name__ == "__main__":
    unittest.main()
