import unittest
from pathlib import Path

from noise_project.analysis import Analyzer, Params, Planner
from noise_project.sources import load_dataset, parse_nights, format_nights
from noise_project.store import Store
from noise_project.errors import StoreError

ROOT = Path(__file__).resolve().parents[1] / "sources"


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(ROOT)
        cls.report = Analyzer(cls.dataset).report()

    # -- evidence: observed vs attributable vs insufficient ------------
    def test_trial_nights_are_the_schedule_union(self):
        self.assertEqual(list(self.dataset.trial_nights), list(range(8, 19)))
        self.assertEqual(list(self.dataset.pretrial_nights), list(range(1, 8)))

    def test_measured_nights_are_not_inflated_to_all_nights(self):
        z1 = next(c for c in self.report.coverage if c.area == "Z1")
        # Two wet nights have a single sensor offline; they are partial, not full.
        self.assertIn(8, z1.partial_coverage_nights)
        self.assertIn(16, z1.partial_coverage_nights)
        self.assertEqual(z1.full_coverage_nights, [9, 10, 11, 12, 13, 14, 15, 17, 18])
        self.assertLess(z1.trial_measured_fraction, 1.0)

    def test_all_outages_fall_on_wet_nights(self):
        design = next(i for i in self.report.insufficient if i["id"] == "wet_night_outage")
        self.assertIn("every sensor outage", design["reasons"][0])
        for coverage in self.report.coverage:
            self.assertEqual(
                coverage.offline_sensor_nights, coverage.wet_offline_sensor_nights
            )

    def test_attribution_uses_energy_scale_and_clean_control(self):
        z1 = next(a for a in self.report.attribution if a.area == "Z1")
        self.assertEqual(z1.status, "validated")
        self.assertGreater(z1.extra_db_mean, 5.0)
        self.assertLess(z1.extra_db_mean, 9.0)
        clean = [c for c in z1.controls if c.clean]
        self.assertEqual([c.sensor for c in clean], ["M6"])
        road = next(c for c in z1.controls if c.sensor == "M5")
        self.assertFalse(road.clean)
        self.assertTrue(road.road_work_nights)

    def test_area_without_exposure_monitor_is_insufficient(self):
        z3 = next(a for a in self.report.attribution if a.area == "Z3")
        self.assertEqual(z3.status, "no_exposure_monitoring")
        self.assertIsNone(z3.extra_db_mean)
        building = self.report.building("L4")
        self.assertFalse(building.verified)
        self.assertTrue(building.coverage_gap_nights)

    def test_multi_operator_area_does_not_count_full_population_per_operator(self):
        z1 = next(a for a in self.report.attribution if a.area == "Z1")
        self.assertEqual(z1.operators_heard, ["U1", "U2"])
        self.assertEqual(z1.comparable_group_residents, 38)
        self.assertEqual(sum(z1.per_operator_residents.values()), 38)
        self.assertEqual(z1.per_operator_residents["U1"], 19)

    def test_single_sensor_nights_do_not_corroborate(self):
        l2 = self.report.building("L2")
        self.assertIn(8, l2.single_sensor_nights)
        self.assertNotIn(8, l2.confirmed_nights)
        self.assertFalse(l2.verified)

    def test_pending_correction_is_flagged_before_it_is_learned(self):
        early = Analyzer(self.dataset).report(asof=20)
        self.assertIn(13, early.building("L2").pending_correction_nights)
        self.assertTrue(early.building("L2").verified)
        late = Analyzer(self.dataset).report(asof=21)
        self.assertFalse(late.building("L2").verified)

    def test_digest_is_stable_for_identical_inputs(self):
        again = Analyzer(self.dataset).report()
        self.assertEqual(self.report.digest, again.digest)
        self.assertEqual(len(self.report.digest), 64)


class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(ROOT)
        cls.report = Analyzer(cls.dataset).report()

    def _planner(self, commitments=None, acceptances=None, budgets=None, baseline=None):
        return Planner(
            self.dataset,
            self.report,
            commitments=commitments or {},
            acceptances=acceptances or [],
            budgets=budgets or {a: b.funding_cents for a, b in self.dataset.budgets.items()},
            baseline_report=baseline,
        )

    def test_multi_operator_night_no_double_claim(self):
        plan = self._planner(budgets={"Z2": 9000}).plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        funded_nights = [n for line in z2.funded if line.kind == "night_reduction" for n in line.nights]
        self.assertEqual(len(funded_nights), len(set(funded_nights)))
        remedies = {line.remedy for line in z2.funded}
        # A1 takes five nights out of the shared 25-29 window, leaving too few
        # distinct nights for A2's four.
        self.assertIn("A1", remedies)
        self.assertNotIn("A2", remedies)

    def test_shared_window_cannot_discharge_two_quotas(self):
        plan = self._planner(budgets={"Z2": 20000}).plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        nights = [n for line in z2.funded for n in line.nights]
        self.assertEqual(len(nights), len(set(nights)))
        self.assertTrue(
            any("distinct night" in item.reason or "window" in item.reason for item in z2.deferred)
        )

    def test_budget_and_slots_are_respected(self):
        plan = self._planner().plan()
        for area in plan.areas:
            self.assertGreaterEqual(area.available_cents, 0)
            self.assertLessEqual(area.slots_used, area.inspection_slots)
            self.assertLessEqual(
                area.reserved_cents + area.allocated_cents, area.funding_cents
            )

    def test_signed_commitment_is_reserved(self):
        commitments = {
            "A3": {
                "remedy": "A3",
                "operator": "U1",
                "day": 18,
                "cents": 3400,
                "units": 1.0,
                "state": "signed",
                "cancelled_day": None,
                "cancelled_confirmed": False,
                "note": "source event S1",
            }
        }
        plan = self._planner(commitments=commitments).plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        self.assertEqual(z2.reserved_cents, 3400)
        self.assertTrue(any(line.remedy == "A3" and line.committed for line in z2.funded))

    def test_cancel_request_does_not_release_funds(self):
        commitments = {
            "A3": {
                "remedy": "A3",
                "operator": "U1",
                "day": 18,
                "cents": 3400,
                "units": 1.0,
                "state": "cancel_requested",
                "cancelled_day": 25,
                "cancelled_confirmed": False,
                "note": "",
            }
        }
        plan = self._planner(commitments=commitments).plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        self.assertEqual(z2.reserved_cents, 3400)
        self.assertTrue(any("stay reserved" in note for note in plan.notes))

    def test_insulation_on_unverified_building_is_withheld(self):
        plan = self._planner().plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        suspended = {item.remedy for item in z2.suspended}
        self.assertTrue({"A3", "A4"} & suspended or {"A3", "A4"} & {item.remedy for item in z2.deferred})
        # L2 is not verified; only the signed A3 may remain as a commitment.
        funded = {line.remedy for line in z2.funded}
        self.assertNotIn("A4", funded)

    def test_boundary_crossing_pauses_new_but_keeps_signed(self):
        early = Analyzer(self.dataset).report(asof=20)
        late = Analyzer(self.dataset).report(asof=21)
        commitments = {
            "A3": {
                "remedy": "A3",
                "operator": "U1",
                "day": 18,
                "cents": 3400,
                "units": 1.0,
                "state": "signed",
                "cancelled_day": None,
                "cancelled_confirmed": False,
                "note": "source event S1",
            }
        }
        plan = Planner(
            self.dataset, late, commitments=commitments, baseline_report=early
        ).plan()
        change = next(item for item in plan.withheld_by_boundary if item["building"] == "L2")
        self.assertEqual(change["change"], "verified_then_not_verified")
        self.assertIn("A3", change["signed_commitments_kept"])
        self.assertIn("A4", change["paused_proposals"])

    def test_acceptance_only_credits_named_building(self):
        plan = self._planner(
            acceptances=[
                {"remedy": "A3", "building": "L2", "units": 0.5, "nights": [], "day": 24}
            ]
        ).plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        delivery = next(d for d in z2.deliveries if d.remedy == "A3")
        self.assertEqual(delivery.outstanding_units, 0.5)
        self.assertIn("L2 only", delivery.scope_note)
        # The neighbouring building is not marked improved by this receipt.
        self.assertEqual(
            [d.remedy for d in z2.deliveries if d.building == "L2"], ["A3"]
        )

    def test_pending_re_measure_names_the_nights(self):
        plan = self._planner().plan()
        l2 = next(item for item in plan.pending_re_measure if item.get("id") == "L2")
        self.assertIn(8, l2["nights_to_re_measure"])

    def test_cross_area_nights_are_unique(self):
        # Add a Z1 night reduction that overlaps A1/A2 in time; no night may be
        # used by two areas' quotas.
        import shutil
        import tempfile

        from noise_project.sources import load_dataset as load

        work = Path(tempfile.mkdtemp())
        for name in ("monitor_locations", "readings", "operator_nights", "measurement_revisions",
                     "beneficiaries", "remedy_offers", "remedy_events", "budget"):
            shutil.copy(ROOT / f"{name}.csv", work / f"{name}.csv")
        with (work / "remedy_offers.csv").open("a", encoding="utf-8") as stream:
            stream.write("A6,U2,Z1,,night_reduction,22,29,4,1500\n")
        dataset = load(work)
        report = Analyzer(dataset).report()
        plan = Planner(dataset, report, budgets={"Z1": 9000, "Z2": 9000}).plan()
        nights = [n for area in plan.areas for line in area.funded for n in line.nights]
        self.assertEqual(len(nights), len(set(nights)))

    def test_confirmed_cancel_releases_reservation(self):
        commitments = {
            "A3": {
                "remedy": "A3",
                "operator": "U1",
                "day": 18,
                "cents": 3400,
                "units": 1.0,
                "state": "cancelled",
                "cancelled_day": 25,
                "cancelled_confirmed": True,
                "note": "",
            }
        }
        plan = self._planner(commitments=commitments).plan()
        z2 = next(a for a in plan.areas if a.area == "Z2")
        self.assertEqual(z2.reserved_cents, 0)
        self.assertFalse(any(line.remedy == "A3" and line.committed for line in z2.funded))


class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.path = Path(tempfile.mkdtemp()) / "state.json"
        self.store = Store(self.path)
        self.dataset = load_dataset(ROOT)

    def test_duplicate_building_acceptance_is_refused(self):
        self.store.add_acceptance("A3", "L2", 0.5, 24)
        with self.assertRaises(StoreError):
            self.store.add_acceptance("A4", "L2", 0.5, 25)

    def test_duplicate_night_acceptance_is_refused(self):
        self.store.add_acceptance("A1", None, 2, 25, nights=[22, 23])
        with self.assertRaises(StoreError):
            self.store.add_acceptance("A2", None, 1, 26, nights=[23])

    def test_cancellation_needs_confirmation_to_release(self):
        self.store.add_commitment("A1", 19, "U1", 2700, 5)
        self.store.request_cancel("A1", 25, "budget")
        merged = self.store.effective_commitments(self.dataset)
        self.assertEqual(merged["A1"]["state"], "cancel_requested")
        self.store.confirm_cancel("A1", 26)
        merged = self.store.effective_commitments(self.dataset)
        self.assertTrue(merged["A1"]["cancelled_confirmed"])

    def test_snapshot_roundtrip_keeps_digest(self):
        report = Analyzer(self.dataset).report()
        snapshot = self.store.add_snapshot("会议口径", report.as_dict())
        self.store.save()
        reopened = Store(self.path)
        self.assertEqual(reopened.snapshot(snapshot["id"])["digest"], report.digest)

    def test_ids_are_unique_per_prefix(self):
        a = self.store.add_correction("M3", 13, 51, 22, "calibration")
        b = self.store.add_correction("M4", 13, 48, 22, "calibration")
        self.assertNotEqual(a["revision"], b["revision"])


class ParsingTests(unittest.TestCase):
    def test_night_ranges(self):
        self.assertEqual(parse_nights("8-10;14"), {8, 9, 10, 14})
        self.assertEqual(format_nights([8, 9, 10, 14]), "8-10,14")
        self.assertEqual(format_nights([]), "-")


if __name__ == "__main__":
    unittest.main()
