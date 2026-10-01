from __future__ import annotations

import json

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.insights.graph import build_receipt_graph
from openshard.insights.query import answer_question
from openshard.insights.warehouse import ReceiptWarehouse
from tests.learning_fixtures import osn_entry


def _write_history(tmp_path):
    history = tmp_path / ".openshard" / "runs.jsonl"
    history.parent.mkdir(parents=True)
    first = osn_entry(
        attempts=[("model/a", "passed")],
        receipt_id="rcpt_" + "a" * 32,
        files=("src/shared.py", "src/a.py"),
        cost=0.02,
        days_ago=2,
        learning={"used": True},
    )
    first["agent"] = "codex"
    first["owner"] = "Michael Obasa"

    second = osn_entry(
        attempts=[("model/b", "failed")],
        receipt_id="rcpt_" + "b" * 32,
        files=("src/shared.py", "src/b.py"),
        cost=0.03,
        days_ago=1,
        learning={"used": False},
    )
    second["agent"] = "claude_code"
    second["owner"] = "Michael Obasa"

    history.write_text(
        "\n".join(json.dumps(entry) for entry in (first, second)) + "\n",
        encoding="utf-8",
    )
    return history


def test_receipt_warehouse_aggregates_observed_evidence(tmp_path):
    _write_history(tmp_path)
    with ReceiptWarehouse(tmp_path) as warehouse:
        overview = warehouse.overview()
        models = warehouse.models()
        costs = warehouse.costs(by="model")
        failures = warehouse.failures()

    assert overview["receipts"] == 2
    assert overview["observed"] == 2
    assert overview["verified_passed"] == 1
    assert overview["verified_failed"] == 1
    assert overview["receipts_with_cost"] == 2
    assert models[0]["model"] == "model/a"
    assert models[0]["pass_rate"] == 1.0
    assert models[1]["model"] == "model/b"
    assert models[1]["pass_rate"] == 0.0
    assert sum(row["known_cost_usd"] or 0 for row in costs) == 0.05
    assert any(row["failure_category"] == "verification_failed" for row in failures)


def test_receipt_graph_links_shared_file_and_evidence_nodes(tmp_path):
    _write_history(tmp_path)
    with ReceiptWarehouse(tmp_path) as warehouse:
        graph = build_receipt_graph(warehouse)

    kinds = {node.kind for node in graph.nodes}
    edge_kinds = {edge.kind for edge in graph.edges}
    assert {"receipt", "agent", "model", "task", "file", "repository"} <= kinds
    assert "CHANGED_FILE" in edge_kinds
    assert "SHARES_CHANGED_FILE" in edge_kinds

    receipt = graph.find_nodes("rcpt_" + "a" * 32, kind="receipt")[0]
    neighborhood = graph.neighborhood(receipt.id, depth=1)
    assert any(node.kind == "model" and node.label == "model/a" for node in neighborhood.nodes)


def test_plain_english_question_returns_supporting_rows(tmp_path):
    _write_history(tmp_path)
    with ReceiptWarehouse(tmp_path) as warehouse:
        answer = answer_question(warehouse, "Which model performs best?")

    assert answer.intent == "model_performance"
    assert "not enough repeated observed runs" in answer.summary
    assert answer.data[0]["observed"] == 1
    assert answer.caveat is not None
    assert "One Receipt" in answer.caveat


def test_insights_cli_json(tmp_path, monkeypatch):
    _write_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["insights", "overview", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["receipts"] == 2
    assert payload["verified_passed"] == 1
    assert payload["verified_failed"] == 1
