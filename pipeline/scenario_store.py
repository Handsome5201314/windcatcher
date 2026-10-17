"""病例内长期研究账本；大几何文件以相对路径和 SHA-256 索引。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import plug_models
from .errors import PipelineError


def record(root: Path, scenario: dict, manifest: dict, event: dict) -> None:
    """在一次 SQLite 事务中保存版本快照和业务事件，不采用监控日志保留期。"""
    database = root.parent.parent / "research.sqlite3"
    if database.is_symlink():
        raise PipelineError("failed", "鼻塞研究数据库路径无效", ["检查病例内 scenarios 目录后重试"])
    encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        with sqlite3.connect(database, timeout=10) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""CREATE TABLE IF NOT EXISTS scenario_versions (
                scenario_id TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
                PRIMARY KEY (scenario_id, version)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS scenario_events (
                event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, scenario_id TEXT NOT NULL,
                version INTEGER NOT NULL, action TEXT NOT NULL, details_json TEXT NOT NULL
            )""")
            connection.execute("""CREATE INDEX IF NOT EXISTS scenario_events_version
                ON scenario_events (scenario_id, version, timestamp)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS scenario_artifacts (
                scenario_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL,
                relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
                document_json TEXT, PRIMARY KEY (scenario_id, version, kind)
            )""")
            connection.execute("""INSERT INTO scenario_versions
                (scenario_id, version, updated_at, snapshot_json, manifest_json) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scenario_id, version) DO UPDATE SET updated_at=excluded.updated_at,
                snapshot_json=excluded.snapshot_json, manifest_json=excluded.manifest_json""", (
                scenario["scenario_id"], scenario["current_version"], scenario["updated_at"],
                encode(scenario), encode(manifest),
            ))
            connection.execute("""INSERT INTO scenario_events
                (event_id, timestamp, scenario_id, version, action, details_json) VALUES (?, ?, ?, ?, ?, ?)""", (
                event["event_id"], event["timestamp"], scenario["scenario_id"],
                int(event["details"].get("version", scenario["current_version"])),
                event["action"], encode(event["details"]),
            ))
            version = int(scenario["current_version"])
            version_root = root / "versions" / f"v{version:04d}"
            artifacts = {
                "original": root / "input" / "original.stl",
                "normalized": root / "normalized" / "model.stl",
                "placed": version_root / "placed" / "model.stl",
                "task": version_root / "scenario_task.json",
                **{name: version_root / "cfd" / f"{name}.json" for name in (
                    "scenario_meta", "run_status", "comparison", "gate",
                )},
            }
            for kind, path in artifacts.items():
                if path.is_symlink() or not path.is_file():
                    continue
                data = path.read_bytes()
                document = data.decode("utf-8") if path.suffix == ".json" else None
                connection.execute("""INSERT INTO scenario_artifacts
                    (scenario_id, version, kind, relative_path, sha256, bytes, document_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(scenario_id, version, kind)
                    DO UPDATE SET relative_path=excluded.relative_path, sha256=excluded.sha256,
                    bytes=excluded.bytes, document_json=excluded.document_json""", (
                    scenario["scenario_id"], version, kind, path.relative_to(root.parent.parent).as_posix(),
                    plug_models.sha256_bytes(data), len(data), document,
                ))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise PipelineError(
            "failed", "鼻塞研究数据库写入失败；本次产物可能已保存，请刷新核对后重试",
            ["检查病例目录权限和磁盘空间", "不要直接重复上传；先刷新方案列表核对已保存版本"],
            {"operation_may_be_saved": True},
        ) from exc
