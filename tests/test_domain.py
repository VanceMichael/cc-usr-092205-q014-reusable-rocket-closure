import copy
import unittest
from pathlib import Path

from src.domain import load_domain, validate_domain

FIXTURE = Path("fixtures/domain.json")


def load_fixture():
    return load_domain(FIXTURE)


class DomainTest(unittest.TestCase):
    def test_fixture_matches_domain(self):
        value = load_fixture()
        self.assertEqual(value["domain"], "reusable-rocket-closure")
        self.assertGreaterEqual(value["version"], 2)
        self.assertGreaterEqual(len(value["constraints"]), 2)

    def test_every_suspect_carries_evidence(self):
        value = load_fixture()
        for node in value["fault_tree"]["nodes"]:
            self.assertTrue(node["evidence"], node["id"])
            for ev in node["evidence"]:
                self.assertIn(ev["kind"], {"supports", "excludes"})

    def test_conflicting_views_allowed_but_conclusion_gated(self):
        value = load_fixture()
        nodes = {n["id"]: n for n in value["fault_tree"]["nodes"]}
        self.assertGreaterEqual(len(nodes["FT-03"]["positions"]), 2)
        self.assertEqual(nodes["FT-03"]["conclusion"]["status"], "unconfirmed")
        self.assertEqual(nodes["FT-03"]["status"], "open")

    def test_confirmed_conclusions_signed_by_independent_reviewer(self):
        value = load_fixture()
        actors = {a["id"]: a for a in value["actors"]}
        for node in value["fault_tree"]["nodes"]:
            if node["conclusion"]["status"] == "confirmed":
                signer = actors[node["conclusion"]["by"]]
                self.assertEqual(signer["kind"], "independent_reviewer")

    def test_high_risk_open_item_blocks_release(self):
        value = load_fixture()
        gate = value["release_gates"][0]
        self.assertEqual(gate["configuration"], "CFG-Y2")
        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(gate["blocked_by"], ["FT-02"])

    def test_readiness_board_splits_by_verification_state(self):
        value = load_fixture()
        board = value["readiness_board"]
        self.assertEqual({e["ref"] for e in board["unverified_hypotheses"]}, {"FT-03"})
        self.assertEqual(
            {e["ref"] for e in board["ground_verified_measures"]}, {"MSR-01", "MSR-03"})
        self.assertEqual(
            {e["ref"] for e in board["needs_flight_verification"]}, {"MSR-02"})

    def test_followups_cover_redecode_failure_split_and_supplier(self):
        value = load_fixture()
        kinds = {f["kind"] for f in value["follow_ups"]}
        self.assertEqual(
            kinds, {"redecode", "test_failure", "measure_split", "supplier_feedback"})

    def test_flight_results_update_states(self):
        value = load_fixture()
        review = value["flight_reviews"][0]
        self.assertEqual(review["flight"], "遥一")
        self.assertEqual({u["to"] for u in review["updates"]}, {"open"})

    def test_owner_and_reviewer_must_differ(self):
        tampered = copy.deepcopy(load_fixture())
        tampered["measures"][0]["reviewer"] = tampered["measures"][0]["owner"]
        with self.assertRaises(ValueError):
            validate_domain(tampered)

    def test_unconfirmed_conclusion_cannot_be_shared(self):
        tampered = copy.deepcopy(load_fixture())
        node = next(n for n in tampered["fault_tree"]["nodes"] if n["id"] == "FT-03")
        node["conclusion"] = {"status": "confirmed", "by": "ACT-REV-01"}
        with self.assertRaises(ValueError):
            validate_domain(tampered)

    def test_release_gate_must_cover_high_risk_items(self):
        tampered = copy.deepcopy(load_fixture())
        tampered["release_gates"][0]["blocked_by"] = []
        tampered["release_gates"][0]["status"] = "released"
        with self.assertRaises(ValueError):
            validate_domain(tampered)

    def test_closed_node_requires_closed_measures(self):
        tampered = copy.deepcopy(load_fixture())
        measure = next(m for m in tampered["measures"] if m["id"] == "MSR-01")
        measure["status"] = "in_progress"
        with self.assertRaises(ValueError):
            validate_domain(tampered)

    def test_measure_split_must_point_back_to_parent(self):
        tampered = copy.deepcopy(load_fixture())
        measure = next(m for m in tampered["measures"] if m["id"] == "MSR-04")
        measure["parent"] = None
        with self.assertRaises(ValueError):
            validate_domain(tampered)


if __name__ == "__main__":
    unittest.main()
