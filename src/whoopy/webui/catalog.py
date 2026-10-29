"""Rebuildable SQLite index for the permanent local application.

Run directories and their JSON manifests remain authoritative. The catalog is
only a disposable search/settings/evaluation projection and can be recreated
at any time without losing a meditation.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CATALOG_SCHEMA_VERSION = 4


class LocalCatalog:
    """Small stdlib-only SQLite catalog with explicit schema ownership."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    audience TEXT NOT NULL DEFAULT 'studio',
                    tts_model TEXT,
                    voice TEXT,
                    duration_seconds REAL,
                    quality_passed INTEGER,
                    has_audio INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_updated_at ON runs(updated_at DESC);
                CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            run_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "audience" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN audience TEXT NOT NULL DEFAULT 'studio'"
                )
            stored_version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if stored_version is None or stored_version["value"] != str(CATALOG_SCHEMA_VERSION):
                # Lab rows are projections of immutable JSON. Recreate only
                # these tables so schema upgrades cannot lose runs/settings.
                connection.executescript(
                    """
                    DROP TABLE IF EXISTS decisions;
                    DROP TABLE IF EXISTS ratings;
                    DROP TABLE IF EXISTS experiments;
                    """
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT NOT NULL,
                    experiment_version INTEGER NOT NULL CHECK(experiment_version >= 1),
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    definition_path TEXT NOT NULL,
                    report_path TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, experiment_version)
                );
                CREATE INDEX IF NOT EXISTS experiments_updated_at
                    ON experiments(updated_at DESC);
                CREATE TABLE IF NOT EXISTS ratings (
                    rating_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    experiment_version INTEGER NOT NULL,
                    assignment_id TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
                    preferred_side TEXT,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id, experiment_version)
                        REFERENCES experiments(experiment_id, experiment_version)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ratings_experiment
                    ON ratings(experiment_id, experiment_version);
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    experiment_version INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    target TEXT NOT NULL,
                    selection_role TEXT,
                    candidate_id TEXT NOT NULL,
                    selected_value TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id, experiment_version)
                        REFERENCES experiments(experiment_id, experiment_version)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS decisions_experiment
                    ON decisions(experiment_id, experiment_version);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(CATALOG_SCHEMA_VERSION),),
            )

    def replace_lab_projection(
        self,
        *,
        experiments: Sequence[Mapping[str, Any]],
        ratings: Sequence[Mapping[str, Any]],
        decisions: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically rebuild Lab search rows from authoritative JSON evidence."""

        experiment_rows = [
            (
                value["experiment_id"],
                value["experiment_version"],
                value["kind"],
                value["status"],
                json.dumps(value["definition"], sort_keys=True, separators=(",", ":")),
                value["definition_path"],
                value.get("report_path"),
                value["updated_at"],
            )
            for value in experiments
        ]
        rating_rows = [
            (
                value["rating_id"],
                value["experiment_id"],
                value["experiment_version"],
                value["assignment_id"],
                value["dimension"],
                value["score"],
                value.get("preferred_side"),
                value.get("notes", ""),
                value["created_at"],
            )
            for value in ratings
        ]
        decision_rows = [
            (
                value["decision_id"],
                value["experiment_id"],
                value["experiment_version"],
                value["outcome"],
                value["target"],
                value.get("selection_role"),
                value["candidate_id"],
                value["selected_value"],
                value["rationale"],
                value["created_at"],
            )
            for value in decisions
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM decisions")
            connection.execute("DELETE FROM ratings")
            connection.execute("DELETE FROM experiments")
            connection.executemany(
                """
                INSERT INTO experiments(
                    experiment_id, experiment_version, kind, status,
                    definition_json, definition_path, report_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                experiment_rows,
            )
            connection.executemany(
                """
                INSERT INTO ratings(
                    rating_id, experiment_id, experiment_version, assignment_id,
                    dimension, score, preferred_side, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rating_rows,
            )
            connection.executemany(
                """
                INSERT INTO decisions(
                    decision_id, experiment_id, experiment_version, outcome,
                    target, selection_role, candidate_id, selected_value, rationale, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                decision_rows,
            )

    def lab_projection(self) -> dict[str, list[dict[str, Any]]]:
        """Return the disposable projection for diagnostics and tests."""

        with self._connect() as connection:
            experiments = connection.execute(
                """
                SELECT * FROM experiments
                ORDER BY experiment_id, experiment_version
                """
            ).fetchall()
            ratings = connection.execute(
                """
                SELECT * FROM ratings
                ORDER BY experiment_id, experiment_version, created_at, rating_id
                """
            ).fetchall()
            decisions = connection.execute(
                """
                SELECT * FROM decisions
                ORDER BY experiment_id, experiment_version, created_at, decision_id
                """
            ).fetchall()
        experiment_values = []
        for row in experiments:
            value = dict(row)
            value["definition"] = json.loads(value.pop("definition_json"))
            experiment_values.append(value)
        return {
            "experiments": experiment_values,
            "ratings": [dict(row) for row in ratings],
            "decisions": [dict(row) for row in decisions],
        }

    def replace_runs(self, runs: Sequence[Mapping[str, Any]]) -> None:
        """Rebuild the search projection from authoritative run summaries."""

        rows = []
        for run in runs:
            rows.append(
                (
                    run["run_id"],
                    run["title"],
                    run["status"],
                    run["source_kind"],
                    run.get("audience", "studio"),
                    run.get("tts_model"),
                    run.get("voice"),
                    run.get("duration_seconds"),
                    None if run.get("quality_passed") is None else int(bool(run["quality_passed"])),
                    int(bool(run.get("has_audio"))),
                    run["created_at"],
                    run["updated_at"],
                    " ".join(
                        str(value or "")
                        for value in (
                            run.get("title"),
                            run.get("source_kind"),
                            run.get("tts_model"),
                            run.get("voice"),
                            run.get("status"),
                        )
                    ).casefold(),
                )
            )
        with self._connect() as connection:
            connection.execute("DELETE FROM runs")
            connection.executemany(
                """
                INSERT INTO runs(
                    run_id, title, status, source_kind, audience, tts_model, voice,
                    duration_seconds, quality_passed, has_audio, created_at,
                    updated_at, search_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def search_runs(
        self,
        *,
        query: str = "",
        audience: str | None = "studio",
        status: str | None = None,
        tts_model: str | None = None,
        minimum_duration_seconds: float | None = None,
        maximum_duration_seconds: float | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["search_text LIKE ?"]
        parameters: list[Any] = [f"%{query.casefold()}%"]
        if audience:
            clauses.append("audience = ?")
            parameters.append(audience)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if tts_model:
            clauses.append("tts_model = ?")
            parameters.append(tts_model)
        if minimum_duration_seconds is not None:
            clauses.append("duration_seconds >= ?")
            parameters.append(minimum_duration_seconds)
        if maximum_duration_seconds is not None:
            clauses.append("duration_seconds <= ?")
            parameters.append(maximum_duration_seconds)
        if created_from is not None:
            clauses.append("julianday(created_at) >= julianday(?)")
            parameters.append(created_from.isoformat())
        if created_to is not None:
            clauses.append("julianday(created_at) <= julianday(?)")
            parameters.append(created_to.isoformat())
        parameters.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            statement = (
                "SELECT * FROM runs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ?"
            )
            rows = connection.execute(
                statement,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "quality_passed": (
                    None if row["quality_passed"] is None else bool(row["quality_passed"])
                ),
                "has_audio": bool(row["has_audio"]),
            }
            for row in rows
        ]

    def export_settings(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, value_json FROM settings ORDER BY key"
            ).fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def set_setting(self, key: str, value: Any) -> None:
        """Upsert one validated setting without replacing unrelated local preferences."""

        if (
            not key
            or len(key) > 100
            or not all(character.isalnum() or character in "._-" for character in key)
        ):
            raise ValueError(f"Invalid settings key {key!r}.")
        try:
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Setting {key!r} is not JSON serializable.") from error
        if len(encoded.encode("utf-8")) > 64_000:
            raise ValueError(f"Setting {key!r} is too large.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, datetime.now(UTC).isoformat()),
            )

    def import_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically replace validated JSON settings in the rebuildable catalog."""

        if len(settings) > 100:
            raise ValueError("Settings import contains too many keys.")
        rows: list[tuple[str, str, str]] = []
        timestamp = datetime.now(UTC).isoformat()
        for key, value in settings.items():
            if (
                not key
                or len(key) > 100
                or not all(character.isalnum() or character in "._-" for character in key)
            ):
                raise ValueError(f"Invalid settings key {key!r}.")
            try:
                encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            except (TypeError, ValueError) as error:
                raise ValueError(f"Setting {key!r} is not JSON serializable.") from error
            if len(encoded.encode("utf-8")) > 64_000:
                raise ValueError(f"Setting {key!r} is too large.")
            rows.append((key, encoded, timestamp))
        with self._connect() as connection:
            connection.execute("DELETE FROM settings")
            connection.executemany(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                rows,
            )
        return self.export_settings()
