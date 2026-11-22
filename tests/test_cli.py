import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from noise_project.cli import main

ROOT = Path(__file__).resolve().parents[1] / "sources"


class CliWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = str(self.dir / "state.json")
        self.base = ["--sources", str(ROOT), "--state", self.state]

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*self.base, *args])
        return code, out.getvalue(), err.getvalue()

    def test_evidence_then_adopt_then_show(self):
        code, out, _ = self.run_cli("evidence", "--asof", "21", "--adopt", "会议口径")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("snapshots")
        self.assertEqual(code, 0)
        self.assertIn("会议口径", out)
        code, out, _ = self.run_cli("show", "S001", "--json")
        self.assertEqual(code, 0)
        snapshot = json.loads(out)
        self.assertEqual(snapshot["asof_day"], 21)
        self.assertTrue(snapshot["digest"])

    def test_plan_json_is_well_formed(self):
        code, out, _ = self.run_cli("plan", "--json")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertIn("areas", plan)
        self.assertTrue(plan["digest"])

    def test_cancel_needs_confirmation_before_release(self):
        self.run_cli("sign", "A1", "--day", "19")
        code, out, _ = self.run_cli("plan", "--json")
        before = json.loads(out)
        z2_before = next(a for a in before["areas"] if a["area"] == "Z2")
        self.assertGreater(z2_before["reserved_cents"], 0)

        self.run_cli("cancel", "A1", "--day", "25", "--reason", "换方案")
        code, out, _ = self.run_cli("plan", "--json")
        still = json.loads(out)
        z2_still = next(a for a in still["areas"] if a["area"] == "Z2")
        self.assertEqual(z2_still["reserved_cents"], z2_before["reserved_cents"])

        self.run_cli("confirm-cancel", "A1", "--day", "26")
        code, out, _ = self.run_cli("plan", "--json")
        released = json.loads(out)
        z2_after = next(a for a in released["areas"] if a["area"] == "Z2")
        self.assertLess(z2_after["reserved_cents"], z2_before["reserved_cents"])

    def test_correction_moves_boundary_and_is_reported(self):
        self.run_cli("correct", "M3", "13", "50", "--learned-day", "22", "--reason", "repair")
        # Before the correction is learned (asof 21) it must be flagged pending.
        code, out, _ = self.run_cli("evidence", "--asof", "21", "--json")
        report = json.loads(out)
        l2 = next(b for b in report["buildings"] if b["building"] == "L2")
        self.assertIn(13, l2["pending_correction_nights"])
        # Once learned (asof 22) the value is applied and no longer pending.
        code, out, _ = self.run_cli("evidence", "--asof", "22", "--json")
        report = json.loads(out)
        l2 = next(b for b in report["buildings"] if b["building"] == "L2")
        self.assertNotIn(13, l2["pending_correction_nights"])

    def test_acceptance_second_operator_on_same_building_refused(self):
        # Source event S3 already accepts A3 on L2; A4 (another operator) may not
        # also claim L2.
        code, out, err = self.run_cli(
            "accept", "A4", "--building", "L2", "--units", "0.5", "--day", "27"
        )
        self.assertEqual(code, 2)
        self.assertIn("L2", err)


if __name__ == "__main__":
    unittest.main()
