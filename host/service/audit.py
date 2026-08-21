"""Utilities for host.service.audit."""
import csv
import io
import json

CSV_HEADER = ["id", "ts", "actor", "ip", "action",
              "target_kind", "target_id", "target_path",
              "result", "err", "detail"]


class AuditService:
    def __init__(self, recorder):
        self.recorder = recorder

    def query(self, from_ms=None, to_ms=None, action=None, device=None,
              result=None, limit=100, offset=0):
        limit = min(max(int(limit or 100), 1), 2000)
        offset = max(int(offset or 0), 0)
        filters = {}
        if from_ms is not None:
            filters["from_ms"] = int(from_ms)
        if to_ms is not None:
            filters["to_ms"] = int(to_ms)
        if action:
            filters["action"] = action
        if device:
            filters["device"] = device
        if result:
            filters["result"] = result
        all_events = self.recorder.query(filters)
        return {"events": all_events[offset:offset + limit],
                "total": len(all_events)}

    def stats(self, days=7):
        return self.recorder.stats(days)

    def export_csv(self, from_ms=None, to_ms=None, limit=50000):
        """Handle export csv."""
        filters = {}
        if from_ms is not None:
            filters["from_ms"] = int(from_ms)
        if to_ms is not None:
            filters["to_ms"] = int(to_ms)
        events = self.recorder.query(filters)[:limit]
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for ev in events:
            t = ev.get("target") or {}
            writer.writerow([
                ev.get("id", ""),
                ev.get("ts", ""),
                ev.get("actor", ""),
                ev.get("ip", ""),
                ev.get("action", ""),
                t.get("kind", ""),
                t.get("id", ""),
                t.get("path", ""),
                ev.get("result", ""),
                ev.get("err", ""),
                json.dumps(ev.get("detail") or {}, ensure_ascii=False),
            ])
        return "\ufeff" + out.getvalue()
