"""MoveLogger: structured records in, MOVELOG-format markdown out.

Continuity with the 2026-11-21 house-survey MOVELOG is a requirement
(PRD §9.4: dataset v2 must be schema-identical to v1), so the markdown
rendering follows the survey's entry shape:

    N. **HH:MM ET — <input>** — WHY: <why> RESULT: <result> (ack X ms)

and each entry is backed by an InputLogRecord carrying the same
fields structurally, for the panel (§4.10) and the dataset builder.
"""

from __future__ import annotations

import json
import threading
import time
from typing import List, Optional

from .state import InputLogRecord

try:  # zoneinfo is stdlib; the log's times are America/New_York like the survey's
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None


def _et_hhmm(ts: float) -> str:
    import datetime

    tz = _ET or datetime.timezone.utc
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%H:%M ET")


def describe_input(record: InputLogRecord) -> str:
    """One-line input description, e.g. 'send_state buttons=[cross]'."""
    inp = record.input or {}
    if "state" in inp:
        st = inp["state"]
        parts = []
        if st.get("buttons"):
            parts.append("buttons=[" + ",".join(st["buttons"]) + "]")
        if st.get("axes"):
            parts.append("axes=" + json.dumps(st["axes"]))
        if st.get("triggers"):
            parts.append("triggers=" + json.dumps(st["triggers"]))
        return "send_state " + (" ".join(parts) if parts else "(neutral)")
    tool = inp.get("tool", "input")
    detail = inp.get("detail", "")
    return f"{tool} {detail}".strip()


class MoveLogger:
    """Appends records to a markdown MOVELOG and keeps them in memory."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.records: List[InputLogRecord] = []
        self._lock = threading.Lock()

    def log(self, record: InputLogRecord) -> InputLogRecord:
        with self._lock:
            self.records.append(record)
            n = len(self.records)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(self.render(record, n) + "\n")
            return record

    def __call__(self, record: InputLogRecord) -> InputLogRecord:
        """So a MoveLogger can be passed directly as a client's log sink."""
        return self.log(record)

    @staticmethod
    def render(record: InputLogRecord, n: int) -> str:
        line = (
            f"{n}. **{_et_hhmm(record.ts)} — {describe_input(record)}** — "
            f"WHY: {record.why} RESULT: {record.result}"
        )
        if record.ack is not None:
            line += f" (ack {record.ack.latency_ms:.1f} ms)"
        return line
