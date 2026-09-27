"""读取并检查可回收火箭归零闭环共享资料。"""

import json
from pathlib import Path

REQUIRED_FIELDS = {
    "domain", "version", "sample_id", "actors", "facts", "constraints",
    "configurations", "telemetry_segments", "environment", "fault_tree",
    "experiments", "measures", "follow_ups", "release_gates",
    "readiness_board", "flight_reviews",
}

CLOSED_NODE_STATES = {"excluded", "closed"}
VERIFIED_MEASURE_STATES = {"ground_verified", "flight_verified"}
FOLLOW_UP_KINDS = {"redecode", "test_failure", "measure_split", "supplier_feedback"}


def load_domain(path: Path) -> dict:
    """读取字段完整且通过闭环规则校验的业务资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_domain(value)
    return value


def validate_domain(value: dict) -> None:
    """校验归零闭环规则，任一规则不满足即抛出 ValueError。"""
    missing = REQUIRED_FIELDS - value.keys()
    if missing:
        raise ValueError(f"共享资料缺少必要字段: {sorted(missing)}")
    if value["version"] < 2:
        raise ValueError("共享资料版本需覆盖工程闭环结构")

    actors = {a["id"]: a for a in value["actors"]}
    if len(actors) != len(value["actors"]):
        raise ValueError("参与方标识重复")
    kinds = {a["kind"] for a in value["actors"]}
    if "measure_owner" not in kinds or "independent_reviewer" not in kinds:
        raise ValueError("措施负责人与独立审查人必须分权设置")

    configurations = {c["id"] for c in value["configurations"]}
    nodes = {n["id"]: n for n in value["fault_tree"]["nodes"]}
    if len(nodes) != len(value["fault_tree"]["nodes"]):
        raise ValueError("故障树节点标识重复")

    telemetry = {t["id"] for t in value["telemetry_segments"]}
    experiments = {e["id"] for e in value["experiments"]}
    environment = {e["id"] for e in value["environment"]}
    follow_ups = {f["id"] for f in value["follow_ups"]}
    evidence_refs = telemetry | experiments | environment | follow_ups

    for node in nodes.values():
        unknown_cfg = set(node["configurations"]) - configurations
        if unknown_cfg:
            raise ValueError(f"{node['id']} 关联了不存在的构型: {sorted(unknown_cfg)}")
        _check_node(node, actors, evidence_refs)

    for exp in value["experiments"]:
        if exp["target_node"] not in nodes:
            raise ValueError(f"{exp['id']} 复现实验指向不存在的嫌疑项")

    measures = {m["id"]: m for m in value["measures"]}
    if len(measures) != len(value["measures"]):
        raise ValueError("措施标识重复")
    for measure in measures.values():
        _check_measure(measure, actors, nodes)

    _check_closure(nodes.values(), measures.values())
    _check_follow_ups(value["follow_ups"], actors, telemetry, experiments, measures)
    _check_release_gates(value["release_gates"], nodes.values(), configurations)
    _check_readiness_board(value["readiness_board"], actors, nodes, measures)
    _check_flight_reviews(value["flight_reviews"], nodes, measures)


def _check_node(node: dict, actors: dict, evidence_refs: set) -> None:
    if not node["evidence"]:
        raise ValueError(f"{node['id']} 嫌疑项必须写明支持或排除证据")
    kinds = set()
    for ev in node["evidence"]:
        if ev["kind"] not in {"supports", "excludes"}:
            raise ValueError(f"{node['id']} 证据类型无效: {ev['kind']}")
        kinds.add(ev["kind"])
        source = ev["source"]
        if not source.startswith("DOC-") and source not in evidence_refs:
            raise ValueError(f"{node['id']} 证据来源无法追溯: {source}")
    status = node["status"]
    if status == "excluded" and "excludes" not in kinds:
        raise ValueError(f"{node['id']} 已排除但缺少排除证据")
    if status in {"confirmed", "closed"} and "supports" not in kinds:
        raise ValueError(f"{node['id']} 已确认但缺少支持证据")
    conclusion = node["conclusion"]
    if status == "open" and conclusion["status"] != "unconfirmed":
        raise ValueError(f"{node['id']} 未证实假设不得共享为已确认结论")
    if conclusion["status"] == "confirmed":
        signer = actors.get(conclusion.get("by", ""))
        if signer is None or signer["kind"] != "independent_reviewer":
            raise ValueError(f"{node['id']} 结论确认必须由独立审查人签署")


def _check_measure(measure: dict, actors: dict, nodes: dict) -> None:
    owner = actors.get(measure["owner"])
    reviewer = actors.get(measure["reviewer"])
    if owner is None or owner["kind"] != "measure_owner":
        raise ValueError(f"{measure['id']} 负责人必须是措施负责人")
    if reviewer is None or reviewer["kind"] != "independent_reviewer":
        raise ValueError(f"{measure['id']} 审查必须由独立审查人承担")
    if measure["owner"] == measure["reviewer"]:
        raise ValueError(f"{measure['id']} 负责人与审查人不得兼任")
    for ref in measure["addresses"]:
        if ref not in nodes:
            raise ValueError(f"{measure['id']} 指向不存在的嫌疑项: {ref}")
    if measure["status"] == "closed" and measure["verification"] not in VERIFIED_MEASURE_STATES:
        raise ValueError(f"{measure['id']} 未验证的措施不得关闭")


def _check_closure(nodes, measures) -> None:
    for node in nodes:
        if node["status"] != "closed":
            continue
        open_measures = [m["id"] for m in measures
                         if node["id"] in m["addresses"] and m["status"] != "closed"]
        if open_measures:
            raise ValueError(f"{node['id']} 关闭前相关措施必须全部关闭: {open_measures}")


def _check_follow_ups(follow_ups, actors, telemetry, experiments, measures) -> None:
    pools = {
        "redecode": telemetry,
        "test_failure": experiments,
        "measure_split": set(measures),
        "supplier_feedback": set(measures),
    }
    for fu in follow_ups:
        kind = fu["kind"]
        if kind not in FOLLOW_UP_KINDS:
            raise ValueError(f"{fu['id']} 后续记录类型无效: {kind}")
        if fu["ref"] not in pools[kind]:
            raise ValueError(f"{fu['id']} 后续记录指向无法追溯: {fu['ref']}")
        if fu["by"] not in actors:
            raise ValueError(f"{fu['id']} 记录人不存在: {fu['by']}")
        if kind == "measure_split":
            for child in fu.get("produces", []):
                if measures.get(child, {}).get("parent") != fu["ref"]:
                    raise ValueError(f"{fu['id']} 拆分出的措施 {child} 未回指原措施")


def _check_release_gates(gates, nodes, configurations) -> None:
    for gate in gates:
        if gate["configuration"] not in configurations:
            raise ValueError(f"放行门槛指向不存在的构型: {gate['configuration']}")
        expected = sorted(
            n["id"] for n in nodes
            if n["risk"] == "high"
            and n["status"] not in CLOSED_NODE_STATES
            and gate["configuration"] in n["configurations"]
        )
        if sorted(gate["blocked_by"]) != expected:
            raise ValueError(f"{gate['configuration']} 放行状态未覆盖全部未关闭高风险项")
        if (gate["status"] == "blocked") != bool(expected):
            raise ValueError(f"{gate['configuration']} 放行状态与高风险项不一致")


def _check_readiness_board(board, actors, nodes, measures) -> None:
    def refs(entries):
        result = set()
        for entry in entries:
            if entry["owner"] not in actors:
                raise ValueError(f"准备会视图条目 {entry['ref']} 缺少责任人")
            result.add(entry["ref"])
        return result

    open_nodes = {n["id"] for n in nodes.values() if n["status"] == "open"}
    if refs(board["unverified_hypotheses"]) != open_nodes:
        raise ValueError("准备会视图未列出全部尚未证实的假设")
    ground = {m["id"] for m in measures.values() if m["verification"] == "ground_verified"}
    if refs(board["ground_verified_measures"]) != ground:
        raise ValueError("准备会视图未列出全部已地面验证的措施")
    needs_flight = {m["id"] for m in measures.values() if m["verification"] == "needs_flight"}
    if refs(board["needs_flight_verification"]) != needs_flight:
        raise ValueError("准备会视图未列出全部仍需飞行验证的措施")


def _check_flight_reviews(reviews, nodes, measures) -> None:
    valid_refs = set(nodes) | set(measures)
    for review in reviews:
        for update in review["updates"]:
            if update["ref"] not in valid_refs:
                raise ValueError(f"{review['flight']} 飞行结果更新指向无法追溯: {update['ref']}")
