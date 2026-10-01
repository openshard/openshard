"""Embedded analytics over the canonical local Receipt history.

The source of truth remains .openshard/runs.jsonl. This module creates an
in-memory DuckDB projection on demand and never writes back to history, so
analytics cannot silently become a second Receipt store.

Only bounded Receipt projections and structured evidence cross into the
warehouse. Raw prompts, transcripts, command output and environment values
never do.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from openshard.history.failures import classify_failure
from openshard.history.run_cost import run_total_cost
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.store import load_history
from openshard.history.views import receipt_to_dict
from openshard.learning.signals import task_category_for
from openshard.routing.adaptive.outcome import outcome_from_receipt


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


class ReceiptWarehouse:
    """A disposable, read-only analytical projection of Receipt history."""

    def __init__(self, root: Path, *, runs_path: Path | None = None):
        self.root = Path(root)
        self.runs_path = runs_path or self.root / ".openshard" / "runs.jsonl"
        self.conn = duckdb.connect(database=":memory:")
        self._load()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ReceiptWarehouse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _load(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE receipts (
                receipt_id VARCHAR,
                shard_id VARCHAR,
                created_at TIMESTAMP,
                owner VARCHAR,
                agent VARCHAR,
                model VARCHAR,
                repo VARCHAR,
                repo_identity VARCHAR,
                task_title VARCHAR,
                task_category VARCHAR,
                verification_status VARCHAR,
                verification_source VARCHAR,
                verified_success BOOLEAN,
                cost_usd DOUBLE,
                cost_complete BOOLEAN,
                duration_seconds DOUBLE,
                tokens_input BIGINT,
                tokens_output BIGINT,
                tokens_cache_read BIGINT,
                tokens_cache_creation BIGINT,
                retry_observed BOOLEAN,
                failure_category VARCHAR,
                learning_used BOOLEAN,
                routing_applied BOOLEAN,
                capture_depth VARCHAR,
                risk VARCHAR
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE files (
                receipt_id VARCHAR,
                path VARCHAR,
                change_type VARCHAR,
                attribution VARCHAR
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE checks (
                receipt_id VARCHAR,
                name VARCHAR,
                kind VARCHAR,
                status VARCHAR
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE policies (
                receipt_id VARCHAR,
                decision VARCHAR,
                area VARCHAR
            )
            """
        )

        receipt_rows: list[tuple[Any, ...]] = []
        file_rows: list[tuple[Any, ...]] = []
        check_rows: list[tuple[Any, ...]] = []
        policy_rows: list[tuple[Any, ...]] = []

        for index, entry in enumerate(load_history(self.runs_path, coerce=True)):
            try:
                receipt = build_shard_receipt(entry, index=index)
                projected = receipt_to_dict(receipt, extended=True)
                outcome = outcome_from_receipt(entry)
                task_category, _category_source = task_category_for(entry)
                failure = classify_failure(entry, receipt)
                cost_usd, cost_complete = run_total_cost(entry)
            except Exception:
                continue

            learning = _dict(entry.get("learning"))
            adaptive = _dict(entry.get("adaptive_routing"))
            routing_applied = outcome.record_mode == "applied" or adaptive.get("applied") is True
            model = outcome.final_model or _text(entry.get("execution_model"))
            if model is None:
                shown = _text(projected.get("model"))
                model = shown if shown not in {"Unknown", "Not recorded"} else None

            receipt_rows.append(
                (
                    _text(projected.get("receipt_id")),
                    _text(projected.get("shard_id")),
                    _text(projected.get("created_at")),
                    _text(projected.get("owner")),
                    _text(projected.get("agent")),
                    model,
                    _text(projected.get("repo")),
                    _text(projected.get("repo_identity")),
                    _text(projected.get("task_title")) or _text(projected.get("task_short")),
                    task_category,
                    outcome.verification_status,
                    outcome.verification_source,
                    outcome.verified_success,
                    cost_usd,
                    cost_complete,
                    _number(projected.get("duration_seconds")),
                    _count(projected.get("tokens_input")),
                    _count(projected.get("tokens_output")),
                    _count(projected.get("tokens_cache_read")),
                    _count(projected.get("tokens_cache_creation")),
                    outcome.retry_observed,
                    failure.category,
                    learning.get("used") if isinstance(learning.get("used"), bool) else None,
                    routing_applied,
                    _text(projected.get("capture_depth")),
                    _text(projected.get("risk")),
                )
            )

            rid = _text(projected.get("receipt_id"))
            for item in projected.get("files") or []:
                if not isinstance(item, dict):
                    continue
                path = _text(item.get("path"))
                if rid and path:
                    file_rows.append(
                        (rid, path, _text(item.get("change_type")), _text(item.get("attribution")))
                    )

            verification = _dict(projected.get("verification"))
            for check in verification.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                name = _text(check.get("name"))
                if rid and name:
                    check_rows.append(
                        (rid, name, _text(check.get("kind")), _text(check.get("status")))
                    )

            for policy in projected.get("policy_decisions") or []:
                if not isinstance(policy, dict):
                    continue
                decision = _text(policy.get("decision"))
                area = _text(policy.get("action"))
                if rid and decision:
                    policy_rows.append((rid, decision, area))

        if receipt_rows:
            self.conn.executemany(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                receipt_rows,
            )
        if file_rows:
            self.conn.executemany("INSERT INTO files VALUES (?, ?, ?, ?)", file_rows)
        if check_rows:
            self.conn.executemany("INSERT INTO checks VALUES (?, ?, ?, ?)", check_rows)
        if policy_rows:
            self.conn.executemany("INSERT INTO policies VALUES (?, ?, ?)", policy_rows)

    def overview(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                count(*)::BIGINT AS receipts,
                count(*) FILTER (WHERE verified_success IS NOT NULL)::BIGINT AS observed,
                count(*) FILTER (WHERE verified_success = true)::BIGINT AS verified_passed,
                count(*) FILTER (WHERE verified_success = false)::BIGINT AS verified_failed,
                count(*) FILTER (WHERE cost_usd IS NOT NULL)::BIGINT AS receipts_with_cost,
                sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS known_cost_usd,
                count(DISTINCT agent) FILTER (WHERE agent IS NOT NULL)::BIGINT AS agents,
                count(DISTINCT model) FILTER (WHERE model IS NOT NULL)::BIGINT AS models,
                count(DISTINCT coalesce(repo_identity, repo)) FILTER (
                    WHERE coalesce(repo_identity, repo) IS NOT NULL
                )::BIGINT AS repositories
            FROM receipts
            """
        ).fetchone()
        keys = [
            "receipts", "observed", "verified_passed", "verified_failed",
            "receipts_with_cost", "known_cost_usd", "agents", "models", "repositories",
        ]
        return dict(zip(keys, row, strict=True)) if row else {key: 0 for key in keys}

    def models(self, *, task_category: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE model IS NOT NULL"
        args: list[object] = []
        if task_category:
            where += " AND lower(task_category) = lower(?)"
            args.append(task_category)
        return _rows(
            self.conn.execute(
                f"""
                SELECT
                    model,
                    count(*)::BIGINT AS runs,
                    count(*) FILTER (WHERE verified_success IS NOT NULL)::BIGINT AS observed,
                    count(*) FILTER (WHERE verified_success = true)::BIGINT AS verified_passed,
                    CASE
                        WHEN count(*) FILTER (WHERE verified_success IS NOT NULL) = 0 THEN NULL
                        ELSE round(
                            count(*) FILTER (WHERE verified_success = true)::DOUBLE /
                            count(*) FILTER (WHERE verified_success IS NOT NULL), 4
                        )
                    END AS pass_rate,
                    count(*) FILTER (WHERE retry_observed = true)::BIGINT AS retries,
                    count(*) FILTER (WHERE cost_usd IS NOT NULL)::BIGINT AS runs_with_cost,
                    sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS known_cost_usd,
                    avg(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS avg_known_cost_usd
                FROM receipts
                {where}
                GROUP BY model
                ORDER BY observed DESC, pass_rate DESC NULLS LAST, runs DESC, model
                """,
                args,
            )
        )

    def agents(self) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                """
                SELECT
                    agent,
                    count(*)::BIGINT AS runs,
                    count(*) FILTER (WHERE verified_success IS NOT NULL)::BIGINT AS observed,
                    count(*) FILTER (WHERE verified_success = false)::BIGINT AS verified_failed,
                    CASE
                        WHEN count(*) FILTER (WHERE verified_success IS NOT NULL) = 0 THEN NULL
                        ELSE round(
                            count(*) FILTER (WHERE verified_success = false)::DOUBLE /
                            count(*) FILTER (WHERE verified_success IS NOT NULL), 4
                        )
                    END AS failure_rate,
                    count(*) FILTER (WHERE cost_usd IS NOT NULL)::BIGINT AS runs_with_cost,
                    sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS known_cost_usd
                FROM receipts
                WHERE agent IS NOT NULL
                GROUP BY agent
                ORDER BY failure_rate DESC NULLS LAST, observed DESC, runs DESC, agent
                """
            )
        )

    def costs(self, *, by: str = "model") -> list[dict[str, Any]]:
        columns = {"model": "model", "agent": "agent", "task": "task_category"}
        column = columns.get(by)
        if column is None:
            raise ValueError("by must be model, agent, or task")
        return _rows(
            self.conn.execute(
                f"""
                SELECT
                    {column} AS label,
                    count(*)::BIGINT AS runs,
                    count(*) FILTER (WHERE cost_usd IS NOT NULL)::BIGINT AS runs_with_cost,
                    sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS known_cost_usd,
                    avg(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS avg_known_cost_usd,
                    count(*) FILTER (WHERE cost_complete = false AND cost_usd IS NOT NULL)::BIGINT AS incomplete_cost_totals
                FROM receipts
                WHERE {column} IS NOT NULL
                GROUP BY {column}
                ORDER BY known_cost_usd DESC NULLS LAST, runs_with_cost DESC, label
                """
            )
        )

    def checks(self) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                """
                SELECT
                    name,
                    coalesce(kind, 'other') AS kind,
                    count(DISTINCT receipt_id)::BIGINT AS runs,
                    count(DISTINCT receipt_id) FILTER (WHERE status = 'failed')::BIGINT AS runs_caught,
                    count(DISTINCT receipt_id) FILTER (WHERE status = 'passed')::BIGINT AS runs_passed
                FROM checks
                GROUP BY name, kind
                ORDER BY runs_caught DESC, runs DESC, name
                """
            )
        )

    def failures(self) -> list[dict[str, Any]]:
        return _rows(
            self.conn.execute(
                """
                SELECT
                    failure_category,
                    count(*)::BIGINT AS runs,
                    count(*) FILTER (WHERE verified_success = false)::BIGINT AS observed_failures,
                    count(*) FILTER (WHERE retry_observed = true)::BIGINT AS retried
                FROM receipts
                WHERE failure_category IS NOT NULL
                  AND failure_category <> 'no_failure_detected'
                GROUP BY failure_category
                ORDER BY runs DESC, observed_failures DESC, failure_category
                """
            )
        )

    def learning(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE learning_used IS NOT NULL)::BIGINT AS recorded,
                count(*) FILTER (WHERE learning_used = true)::BIGINT AS used,
                count(*) FILTER (WHERE learning_used = false)::BIGINT AS not_used,
                count(*) FILTER (WHERE learning_used = true AND verified_success = true)::BIGINT AS used_and_verified,
                count(*) FILTER (WHERE routing_applied = true)::BIGINT AS routing_applied
            FROM receipts
            """
        ).fetchone()
        keys = ["recorded", "used", "not_used", "used_and_verified", "routing_applied"]
        return dict(zip(keys, row, strict=True)) if row else {key: 0 for key in keys}

    def task_categories(self) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT task_category FROM receipts WHERE task_category IS NOT NULL ORDER BY task_category"
            ).fetchall()
        ]

    def receipt_rows(self) -> list[dict[str, Any]]:
        return _rows(self.conn.execute("SELECT * FROM receipts ORDER BY created_at, receipt_id"))

    def file_rows(self) -> list[dict[str, Any]]:
        return _rows(self.conn.execute("SELECT * FROM files ORDER BY receipt_id, path"))

    def check_rows(self) -> list[dict[str, Any]]:
        return _rows(self.conn.execute("SELECT * FROM checks ORDER BY receipt_id, name"))

    def policy_rows(self) -> list[dict[str, Any]]:
        return _rows(self.conn.execute("SELECT * FROM policies ORDER BY receipt_id, decision, area"))
