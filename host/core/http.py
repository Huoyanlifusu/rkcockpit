"""Utilities for host.core.http."""
import json


def send_json(handler, code, payload, ctype="application/json; charset=utf-8"):
    if isinstance(payload, str):
        body = payload.encode("utf-8")
    elif isinstance(payload, (dict, list)):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        body = payload
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
    return True


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except ValueError:
        return {}
