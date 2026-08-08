import unittest
from app.quant.decision_score import compute_calibrated_confidence


class TestDecisionScoreCalibration(unittest.TestCase):
    def test_compute_calibrated_confidence_baseline_only(self):
        """Verify calibrated confidence returns baseline when board confidence is None."""
        calibrated = compute_calibrated_confidence(baseline_confidence=65)
        self.assertEqual(calibrated, 65.0)

    def test_compute_calibrated_confidence_blended(self):
        """Verify calibrated confidence correctly weights 55% baseline + 45% board."""
        # 0.55 * 80 + 0.45 * 60 = 44 + 27 = 71.0
        calibrated = compute_calibrated_confidence(baseline_confidence=80, board_confidence=60)
        self.assertEqual(calibrated, 71.0)

    def test_compute_calibrated_confidence_bounds(self):
        """Verify calibrated confidence clamps to [0, 100]."""
        self.assertEqual(compute_calibrated_confidence(baseline_confidence=120, board_confidence=150), 100.0)
        self.assertEqual(compute_calibrated_confidence(baseline_confidence=-20, board_confidence=-10), 0.0)


if __name__ == "__main__":
    unittest.main()
