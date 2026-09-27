import unittest
from pathlib import Path

from src.closure import (
    apply_flight_result,
    load_closure,
    readiness_board,
    release_decision,
    validate_closure,
)

FIXTURE = Path("fixtures/closure.json")


def load_valid():
    return load_closure(FIXTURE)


def find(rows, row_id):
    return next(row for row in rows if row["id"] == row_id)


class ClosureTest(unittest.TestCase):
    def test_fixture_passes_all_checks(self):
        self.assertEqual(validate_closure(load_valid()), [])

    def test_suspect_item_requires_evidence(self):
        data = load_valid()
        find(data["fault_tree"], "FT-05")["evidence"] = []
        self.assertIn("FT-05 嫌疑项缺少支持或排除证据", validate_closure(data))

    def test_excluded_item_requires_excluding_evidence(self):
        data = load_valid()
        item = find(data["fault_tree"], "FT-01")
        item["status"] = "已排除"
        item["evidence"] = [e for e in item["evidence"] if e["kind"] == "支持"]
        problems = validate_closure(data)
        self.assertIn("FT-01 标记已排除但缺少排除证据", problems)

    def test_closed_item_requires_supporting_evidence(self):
        data = load_valid()
        item = find(data["fault_tree"], "FT-02")
        item["status"] = "已关闭"
        problems = validate_closure(data)
        self.assertIn("FT-02 标记已关闭但缺少支持证据", problems)

    def test_closed_item_requires_ground_verified_measure(self):
        data = load_valid()
        find(data["measures"], "M-001")["verifications"] = []
        problems = validate_closure(data)
        self.assertIn("FT-01 关闭所依据的 M-001 未完成地面验证", problems)

    def test_closed_item_requires_independent_review(self):
        data = load_valid()
        data["reviews"] = [r for r in data["reviews"] if r["measure"] != "M-002"]
        problems = validate_closure(data)
        self.assertIn("FT-03 关闭所依据的 M-002 未通过独立审查", problems)

    def test_shared_conclusion_must_be_confirmed(self):
        data = load_valid()
        find(data["assessments"], "AS-03")["status"] = "未确认"
        problems = validate_closure(data)
        self.assertIn("AS-03 多专业共享结论未经确认", problems)

    def test_conflicting_single_discipline_assessments_allowed(self):
        data = load_valid()
        solo = [a for a in data["assessments"] if len(a["disciplines"]) == 1]
        self.assertGreaterEqual(len(solo), 2)
        self.assertTrue(all(a["status"] == "未确认" for a in solo))
        self.assertEqual(validate_closure(data), [])

    def test_failed_experiment_needs_follow_up(self):
        data = load_valid()
        data["follow_ups"] = [
            f for f in data["follow_ups"] if f["kind"] != "试验失败"
        ]
        problems = validate_closure(data)
        self.assertIn("EXP-001 试验失败但未形成后续记录", problems)

    def test_redecoded_telemetry_needs_follow_up(self):
        data = load_valid()
        data["follow_ups"] = [
            f for f in data["follow_ups"] if f["kind"] != "数据重解码"
        ]
        problems = validate_closure(data)
        self.assertIn("TEL-001 重新解码但未形成后续记录", problems)

    def test_split_measure_needs_follow_up(self):
        data = load_valid()
        data["follow_ups"] = [
            f for f in data["follow_ups"] if f["kind"] != "措施拆分"
        ]
        problems = validate_closure(data)
        self.assertIn("M-004 由 M-003 拆分但未形成后续记录", problems)
        self.assertIn("M-005 由 M-003 拆分但未形成后续记录", problems)

    def test_reviewer_must_differ_from_owner(self):
        data = load_valid()
        find(data["reviews"], "RV-001")["reviewer"] = "结构专业-陈工"
        problems = validate_closure(data)
        self.assertIn("RV-001 审查人与措施负责人未分权: 结构专业-陈工", problems)

    def test_unknown_reference_rejected(self):
        data = load_valid()
        find(data["measures"], "M-001")["addresses"] = ["FT-99"]
        problems = validate_closure(data)
        self.assertIn("M-001 引用未知嫌疑项 FT-99", problems)

    def test_open_high_risk_blocks_release(self):
        decision = release_decision(load_valid(), "CFG-B2")
        self.assertEqual(decision["decision"], "阻止")
        self.assertEqual(decision["blocking"], ["FT-05"])

    def test_release_record_must_match_gate(self):
        data = load_valid()
        data["releases"][0]["decision"] = "放行"
        problems = validate_closure(data)
        self.assertIn("CFG-B2 放行结论与门禁不一致: 记录为放行，应为阻止", problems)

    def test_readiness_board(self):
        board = readiness_board(load_valid(), "CFG-B2")
        self.assertEqual(
            [h["id"] for h in board["unproven_hypotheses"]], ["FT-05"]
        )
        self.assertEqual(
            board["unproven_hypotheses"][0]["owner"], "机电专业-赵工"
        )
        self.assertEqual(
            [m["id"] for m in board["ground_verified_measures"]],
            ["M-001", "M-002", "M-005"],
        )
        self.assertEqual(
            [m["id"] for m in board["pending_flight_verification"]],
            ["M-001", "M-002"],
        )
        owners = {m["id"]: m["owner"] for m in board["pending_flight_verification"]}
        self.assertEqual(owners["M-001"], "结构专业-陈工")
        self.assertEqual(owners["M-002"], "控制专业-林工")
        self.assertEqual(board["release"]["decision"], "阻止")

    def close_ft05(self, data):
        find(data["measures"], "M-004")["verifications"].append(
            {"scope": "地面", "method": "批次05装配间隙复测", "result": "通过"}
        )
        data["reviews"].append(
            {
                "id": "RV-004",
                "measure": "M-004",
                "reviewer": "独立审查-李工",
                "result": "通过",
            }
        )
        find(data["fault_tree"], "FT-05")["status"] = "已关闭"
        data["releases"].append(
            {
                "configuration": "CFG-B2",
                "decision": "放行",
                "reason": "FT-05 已关闭，高风险嫌疑项均已关闭或排除",
            }
        )
        return data

    def test_release_after_closing_high_risk(self):
        data = self.close_ft05(load_valid())
        self.assertEqual(validate_closure(data), [])
        self.assertEqual(release_decision(data, "CFG-B2")["decision"], "放行")

    def test_successful_flight_updates_states(self):
        data = self.close_ft05(load_valid())
        updated = apply_flight_result(data, "Y2", "回收成功")
        board = readiness_board(updated, "CFG-B2")
        self.assertEqual(board["pending_flight_verification"], [])
        self.assertEqual(board["release"]["decision"], "放行")
        self.assertTrue(any(f["kind"] == "飞行结果" for f in updated["follow_ups"]))
        self.assertEqual(validate_closure(updated), [])
        # 原记录不被改动
        self.assertEqual(validate_closure(data), [])
        self.assertNotEqual(data["follow_ups"], updated["follow_ups"])

    def test_failed_flight_reopens_items(self):
        data = self.close_ft05(load_valid())
        updated = apply_flight_result(data, "Y2", "回收失败")
        statuses = {i["id"]: i["status"] for i in updated["fault_tree"]}
        self.assertEqual(statuses["FT-01"], "调查中")
        self.assertEqual(statuses["FT-03"], "调查中")
        self.assertEqual(statuses["FT-05"], "调查中")
        self.assertEqual(release_decision(updated, "CFG-B2")["decision"], "阻止")
        # 飞行失败后原放行结论作废，校验必须标出
        self.assertEqual(
            validate_closure(updated),
            ["CFG-B2 放行结论与门禁不一致: 记录为放行，应为阻止"],
        )


if __name__ == "__main__":
    unittest.main()
