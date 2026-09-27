"""可回收火箭归零闭环：加载、校验、放行门禁与准备会视图。

闭环记录覆盖遥测片段、飞行构型、软件版本、环境、故障树嫌疑项、
复现实验、改进措施、多专业结论、后续记录、独立审查和构型放行。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

CLOSED_STATES = {"已排除", "已关闭"}
OPEN_STATES = {"待排查", "调查中"}
EVIDENCE_KINDS = {"支持", "排除"}
FOLLOW_UP_KINDS = {"数据重解码", "试验失败", "措施拆分", "供应商反馈", "飞行结果"}

REQUIRED_FIELDS = {
    "domain",
    "version",
    "sample_id",
    "investigation",
    "telemetry",
    "configurations",
    "software",
    "environments",
    "fault_tree",
    "experiments",
    "measures",
    "assessments",
    "follow_ups",
    "reviews",
    "releases",
}


def load_closure(path: Path) -> dict:
    """读取结构完整的归零闭环记录。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        raise ValueError(f"闭环记录缺少必要字段: {sorted(missing)}")
    if data["version"] < 2:
        raise ValueError("闭环记录版本过低")
    return data


def ground_verified(measure: dict) -> bool:
    """措施已通过地面验证。"""
    return any(
        v["scope"] == "地面" and v["result"] == "通过"
        for v in measure.get("verifications", [])
    )


def flight_verified(measure: dict) -> bool:
    """措施已通过飞行验证。"""
    return any(
        v["scope"] == "飞行" and v["result"] == "通过"
        for v in measure.get("verifications", [])
    )


def pending_flight(measure: dict) -> bool:
    """措施已地面验证但仍需飞行验证。"""
    return (
        ground_verified(measure)
        and measure.get("requires_flight", False)
        and not flight_verified(measure)
    )


def _review_passed(measure: dict, reviews: list) -> bool:
    return any(
        r["measure"] == measure["id"] and r["result"] == "通过" for r in reviews
    )


def validate_closure(data: dict) -> list:
    """逐条检查闭环规则，返回问题清单（空清单表示通过）。"""
    problems = []
    problems += check_references(data)
    problems += check_suspect_evidence(data)
    problems += check_closure_requirements(data)
    problems += check_shared_conclusions(data)
    problems += check_follow_up_traces(data)
    problems += check_separation_of_duties(data)
    problems += check_release_consistency(data)
    return problems


def check_references(data: dict) -> list:
    """记录之间的引用必须存在。"""
    problems = []
    item_ids = {i["id"] for i in data["fault_tree"]}
    cfg_ids = {c["id"] for c in data["configurations"]}
    measure_ids = {m["id"] for m in data["measures"]}
    for item in data["fault_tree"]:
        for cfg in item.get("configurations", []):
            if cfg not in cfg_ids:
                problems.append(f"{item['id']} 引用未知构型 {cfg}")
    for measure in data["measures"]:
        for target in measure.get("addresses", []):
            if target not in item_ids:
                problems.append(f"{measure['id']} 引用未知嫌疑项 {target}")
        if measure.get("split_from") and measure["split_from"] not in measure_ids:
            problems.append(f"{measure['id']} 引用未知来源措施 {measure['split_from']}")
    for review in data["reviews"]:
        if review["measure"] not in measure_ids:
            problems.append(f"{review['id']} 引用未知措施 {review['measure']}")
    for release in data["releases"]:
        if release["configuration"] not in cfg_ids:
            problems.append(f"放行记录引用未知构型 {release['configuration']}")
    return problems


def check_suspect_evidence(data: dict) -> list:
    """每个嫌疑项必须写明支持或排除证据，且状态与证据一致。"""
    problems = []
    for item in data["fault_tree"]:
        evidence = item.get("evidence", [])
        if not evidence:
            problems.append(f"{item['id']} 嫌疑项缺少支持或排除证据")
        kinds = set()
        for entry in evidence:
            if entry.get("kind") not in EVIDENCE_KINDS:
                problems.append(f"{item['id']} 证据类型无效: {entry.get('kind')}")
            kinds.add(entry.get("kind"))
        if item["status"] == "已排除" and "排除" not in kinds:
            problems.append(f"{item['id']} 标记已排除但缺少排除证据")
        if item["status"] == "已关闭" and "支持" not in kinds:
            problems.append(f"{item['id']} 标记已关闭但缺少支持证据")
    return problems


def check_closure_requirements(data: dict) -> list:
    """嫌疑项关闭前，关联措施须完成地面验证并通过独立审查。

    已拆分的原始措施由拆分后的子措施承接验证责任，不再单独要求。
    """
    problems = []
    measures = data["measures"]
    for item in data["fault_tree"]:
        if item["status"] != "已关闭":
            continue
        linked = [
            m
            for m in measures
            if item["id"] in m.get("addresses", []) and not m.get("split_into")
        ]
        if not linked:
            problems.append(f"{item['id']} 已关闭但无关联改进措施")
        for measure in linked:
            if not ground_verified(measure):
                problems.append(
                    f"{item['id']} 关闭所依据的 {measure['id']} 未完成地面验证"
                )
            if not _review_passed(measure, data["reviews"]):
                problems.append(
                    f"{item['id']} 关闭所依据的 {measure['id']} 未通过独立审查"
                )
    return problems


def check_shared_conclusions(data: dict) -> list:
    """多专业并行调查允许结论冲突，但共享结论必须经确认。"""
    problems = []
    for assessment in data["assessments"]:
        if len(assessment.get("disciplines", [])) > 1 and assessment["status"] != "已确认":
            problems.append(f"{assessment['id']} 多专业共享结论未经确认")
    return problems


def check_follow_up_traces(data: dict) -> list:
    """数据重解码、试验失败、措施拆分、供应商反馈均须形成后续记录。"""
    problems = []
    follow_ups = data["follow_ups"]
    ids = [f["id"] for f in follow_ups]
    if len(ids) != len(set(ids)):
        problems.append("后续记录编号重复")
    for record in follow_ups:
        if record.get("kind") not in FOLLOW_UP_KINDS:
            problems.append(f"{record['id']} 后续记录类型无效: {record.get('kind')}")

    def has_record(kind, ref):
        return any(f["kind"] == kind and f["ref"] == ref for f in follow_ups)

    for segment in data["telemetry"]:
        if segment.get("decoding_version", 1) > 1 and not has_record("数据重解码", segment["id"]):
            problems.append(f"{segment['id']} 重新解码但未形成后续记录")
    for experiment in data["experiments"]:
        if experiment["result"] == "失败" and not has_record("试验失败", experiment["id"]):
            problems.append(f"{experiment['id']} 试验失败但未形成后续记录")
    for measure in data["measures"]:
        if measure.get("split_from") and not has_record("措施拆分", measure["split_from"]):
            problems.append(
                f"{measure['id']} 由 {measure['split_from']} 拆分但未形成后续记录"
            )
    return problems


def check_separation_of_duties(data: dict) -> list:
    """独立审查人不得与措施负责人为同一人。"""
    problems = []
    owners = {m["id"]: m["owner"] for m in data["measures"]}
    for review in data["reviews"]:
        if review["reviewer"] == owners.get(review["measure"]):
            problems.append(
                f"{review['id']} 审查人与措施负责人未分权: {review['reviewer']}"
            )
    return problems


def blocking_items(data: dict, configuration: str) -> list:
    """未关闭的高风险嫌疑项，阻止对应构型放行。"""
    return [
        item
        for item in data["fault_tree"]
        if item["risk"] == "高"
        and item["status"] not in CLOSED_STATES
        and configuration in item.get("configurations", [])
    ]


def release_decision(data: dict, configuration: str) -> dict:
    """按放行门禁计算构型放行结论。"""
    blocked = blocking_items(data, configuration)
    if blocked:
        ids = "、".join(item["id"] for item in blocked)
        return {
            "decision": "阻止",
            "reason": f"高风险嫌疑项未关闭: {ids}",
            "blocking": [item["id"] for item in blocked],
        }
    return {"decision": "放行", "reason": "高风险嫌疑项均已关闭或排除", "blocking": []}


def check_release_consistency(data: dict) -> list:
    """每个构型的最新放行记录必须与放行门禁一致，阻止须写明原因。"""
    problems = []
    cfg_ids = {c["id"] for c in data["configurations"]}
    latest = {}
    for release in data["releases"]:
        latest[release["configuration"]] = release
    for configuration, release in latest.items():
        if configuration not in cfg_ids:
            continue
        expected = release_decision(data, configuration)
        if release["decision"] != expected["decision"]:
            problems.append(
                f"{configuration} 放行结论与门禁不一致: "
                f"记录为{release['decision']}，应为{expected['decision']}"
            )
        if release["decision"] == "阻止" and not release.get("reason"):
            problems.append(f"{configuration} 阻止放行但未写明原因")
    return problems


def readiness_board(data: dict, configuration: str) -> dict:
    """下一发准备会视图：未证实假设、地面已验证措施、待飞行验证部分及责任人。"""
    items = [
        item
        for item in data["fault_tree"]
        if configuration in item.get("configurations", [])
    ]
    item_ids = {item["id"] for item in items}
    measures = [
        m
        for m in data["measures"]
        if item_ids & set(m.get("addresses", []))
    ]
    return {
        "configuration": configuration,
        "unproven_hypotheses": [
            {"id": i["id"], "title": i["title"], "owner": i["owner"]}
            for i in items
            if i["status"] in OPEN_STATES
        ],
        "ground_verified_measures": [
            {"id": m["id"], "title": m["title"], "owner": m["owner"]}
            for m in measures
            if ground_verified(m)
        ],
        "pending_flight_verification": [
            {"id": m["id"], "title": m["title"], "owner": m["owner"]}
            for m in measures
            if pending_flight(m)
        ],
        "release": release_decision(data, configuration),
    }


def apply_flight_result(data: dict, mission: str, outcome: str, note: str = "") -> dict:
    """飞行结果回来后更新措施与嫌疑项状态，并追加后续记录。

    回收成功：待飞行验证的措施记为飞行验证通过。
    回收失败：相关嫌疑项重新打开，等待新一轮归零。
    返回更新后的副本，不改动原记录。
    """
    updated = copy.deepcopy(data)
    success = outcome == "回收成功"
    result = "通过" if success else "失败"
    touched = []
    for measure in updated["measures"]:
        if pending_flight(measure):
            measure.setdefault("verifications", []).append(
                {"scope": "飞行", "method": f"{mission} 飞行验证", "result": result}
            )
            touched.append(measure["id"])
    if not success:
        failed = set(touched)
        for item in updated["fault_tree"]:
            if item["status"] != "已关闭":
                continue
            linked = {
                m["id"]
                for m in updated["measures"]
                if item["id"] in m.get("addresses", [])
            }
            if linked & failed:
                item["status"] = "调查中"
    seq = len(updated["follow_ups"]) + 1
    updated["follow_ups"].append(
        {
            "id": f"FU-{seq:03d}",
            "kind": "飞行结果",
            "ref": mission,
            "note": note or f"{mission} 飞行结果: {outcome}",
            "recorded_by": "型号办公室",
        }
    )
    return updated
