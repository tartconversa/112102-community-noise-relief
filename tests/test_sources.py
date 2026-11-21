import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "sources"
HEADERS = {'monitor_locations': ['sensor', 'area', 'site_a', 'site_b', 'role'], 'readings': ['sensor', 'night', 'observed_db', 'background_db', 'state', 'weather', 'road_work'], 'operator_nights': ['operator', 'first_night', 'last_night', 'nights_excluded'], 'measurement_revisions': ['revision', 'sensor', 'night', 'corrected_db', 'learned_day', 'reason'], 'beneficiaries': ['building', 'area', 'resident_weight', 'eligibility_cutoff_db'], 'remedy_offers': ['remedy', 'operator', 'area', 'building', 'kind', 'first_night', 'last_night', 'units', 'cents'], 'remedy_events': ['event', 'remedy', 'day', 'state', 'actual_building', 'actual_units'], 'budget': ['area', 'funding_cents', 'inspection_slots']}
REFERENCES = [['readings', 'sensor', 'monitor_locations', 'sensor'], ['measurement_revisions', 'sensor', 'monitor_locations', 'sensor'], ['remedy_events', 'remedy', 'remedy_offers', 'remedy'], ['remedy_events', 'actual_building', 'beneficiaries', 'building']]
COUNTS = [['readings', 'state', 'offline', 1], ['remedy_events', 'state', 'partial_acceptance', 1], ['remedy_offers', 'building', 'L2', 2]]


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        for name, header in HEADERS.items():
            with (ROOT / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, header)
                self.records[name] = list(reader)

    def test_record_shape(self):
        for name, records in self.records.items():
            with self.subTest(source=name):
                self.assertTrue(records)
                for row in records:
                    self.assertEqual(set(row), set(HEADERS[name]))
                    self.assertNotIn(None, row.values())
                    self.assertTrue(row[HEADERS[name][0]])
                self.assertEqual(len(records), len({tuple(row.values()) for row in records}))

    def test_cross_source_references(self):
        for child, fk, parent, pk in REFERENCES:
            with self.subTest(source=child, field=fk):
                keys = {row[pk] for row in self.records[parent]}
                values = {row[fk] for row in self.records[child] if row[fk]}
                self.assertTrue(values)
                self.assertFalse(values - keys)

    def test_source_has_relevant_observations(self):
        for source, field, value, minimum in COUNTS:
            with self.subTest(source=source, field=field, value=value):
                self.assertGreaterEqual(sum(row[field] == value for row in self.records[source]), minimum)
