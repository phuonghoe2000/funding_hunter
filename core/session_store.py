from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


class SessionStore:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.path.join(_project_root(), "runtime")
        self.active_session_path = os.path.join(self.base_dir, "active_session.json")
        self.trade_journal_path = os.path.join(self.base_dir, "trade_journal.jsonl")
        os.makedirs(self.base_dir, exist_ok=True)

    @staticmethod
    def new_session_id() -> str:
        return uuid4().hex

    def save_active_session(self, session: dict[str, Any]) -> None:
        payload = _serialize(session)
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.active_session_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def load_active_session(self) -> Optional[dict[str, Any]]:
        if not os.path.exists(self.active_session_path):
            return None
        with open(self.active_session_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def clear_active_session(self) -> None:
        if os.path.exists(self.active_session_path):
            os.remove(self.active_session_path)

    def append_journal_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": _serialize(payload),
        }
        with open(self.trade_journal_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read_recent_journal(self, limit: int = 50) -> list[dict[str, Any]]:
        if not os.path.exists(self.trade_journal_path):
            return []
        with open(self.trade_journal_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        recent_lines = lines[-limit:]
        return [json.loads(line) for line in recent_lines if line.strip()]
