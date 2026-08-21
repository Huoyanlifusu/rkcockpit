import argparse
import ipaddress
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERSION = "0.2.0"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from host import discovery
from host.api import HostApi, host_api_dispatch
from host.api.router import dispatch as router_dispatch, load_handlers
from host.core.auth import AuthConfigError, TokenAuth
from host.core.metrics import METRICS
from portal.server import BoundedThreadingHTTPServer
from portal.sse import HUB

STATIC_DIR = os.path.join(BASE_DIR, "static")
DISCOVER_INTERVAL = 15.0
DISCOVER_TTL = 5.0
DEFAULT_DISCOVER_RULES = {
    "ips": [],
    "serials": [],
    "banner_exclude": ["for_windows", "win32"],
}
MAX_JSON_BODY = 1 << 20
METRICS_CACHE_SECONDS = 1.0


def now_ms():
    return int(time.time() * 1000)


def runtime_metrics_snapshot(host):
    """Overlay live SSE/audit state without history scans or disk/network I/O."""
    snapshot = METRICS.snapshot()
    sse = HUB.stats()
    snapshot["sse"].update({
        "active": sse["active"],
        "active_by_kind": sse["active_by_kind"],
        "accepted_total": sse["accepted"],
        "rejected_total": sse["rejected"],
        "rejected_by_kind": sse["rejected_by_kind"],
        "write_timeout_total": sse["write_timeout"],
    })
    runtime_stats = getattr(getattr(host, "audit", None),
                            "runtime_stats", None)
    if runtime_stats is not None:
        audit = runtime_stats()
        snapshot["audit"].update({
            "queue_depth": audit["queue"],
            "queue_capacity": audit["queue_capacity"],
            "pending": audit["pending"],
            "enqueued_total": audit["enqueued"],
            "fallback_total": audit["fallback"],
            "written_total": audit["written"],
            "write_failure_total": audit["write_failure"],
            "unpersisted_total": audit["unpersisted"],
            "invalid_total": audit["invalid"],
            "degraded": bool(audit["degraded"]),
            "accepting": bool(audit["accepting"]),
        })
    scheduler_stats = getattr(getattr(host, "ssh_scheduler", None),
                              "stats", None)
    if scheduler_stats is not None:
        snapshot["ssh"]["scheduler"] = scheduler_stats()
    spool_stats = getattr(getattr(host, "upload_spool", None), "stats", None)
    if spool_stats is not None:
        snapshot["upload_spool"] = spool_stats()
    return snapshot


def cached_runtime_metrics_snapshot(server, clock=time.monotonic):
    """Bound expensive fixed-schema overlays to once per second.

    Metrics are operational samples rather than transactional state.  A short
    cache prevents a polling client from repeatedly scanning /proc and sorting
    scheduler samples while preserving bounded, fixed-cardinality output.
    """
    now = clock()
    with server.metrics_cache_lock:
        if server.metrics_cache_value is not None and\
                now - server.metrics_cache_time < METRICS_CACHE_SECONDS:
            return server.metrics_cache_value
        value = runtime_metrics_snapshot(server.host)
        server.metrics_cache_value = value
        server.metrics_cache_time = now
        return value


class RulesStore:

    def __init__(self, conf_dir):
        self.path = os.path.join(conf_dir, "discovery-rules.json")
        self._lock = threading.Lock()
        self._rules = dict(DEFAULT_DISCOVER_RULES)
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, default in DEFAULT_DISCOVER_RULES.items():
            value = raw.get(key)
            if isinstance(value, list):
                cleaned = list(dict.fromkeys(
                    s for s in value if isinstance(s, str) and s.strip()))
                self._rules[key] = cleaned

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._rules, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self):
        with self._lock:
            return dict(self._rules)

    def set(self, rules):
        if not isinstance(rules, dict):
            raise ValueError("rules 必须是对象")
        next_rules = dict(DEFAULT_DISCOVER_RULES)
        for key, default in DEFAULT_DISCOVER_RULES.items():
            value = rules.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or\
                    not all(isinstance(s, str) and s.strip() for s in value):
                raise ValueError("%s 必须是字符串数组且不能有空项" % key)
            next_rules[key] = list(dict.fromkeys(
                s.strip() for s in value))
        with self._lock:
            self._rules = next_rules
            self.save()
        return dict(self._rules)


class DiscoverCache:
    """Manage discover cache."""

    def __init__(self, host_api, rules_store=None):
        self.host_api = host_api
        self.rules_store = rules_store
        self.result = None
        self.last = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True,
                         name="rkss-discover").start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                pass
            self._stop.wait(DISCOVER_INTERVAL)

    def stop(self):
        self._stop.set()

    def _ssh_hosts(self):
        try:
            return [d.get("host") for d in self.host_api.store.list()
                    if d.get("type") == "ssh" and d.get("host")]
        except Exception:
            return []

    def refresh(self):
        rules = self.rules_store.get() if self.rules_store else None
        result = discovery.discover(devices=self._ssh_hosts(), rules=rules)
        with self._lock:
            self.result = result
            self.last = time.time()
        return result

    def get(self, force=False):
        with self._lock:
            fresh = self.result is not None and\
                time.time() - self.last < DISCOVER_TTL
        if not force and fresh:
            with self._lock:
                return {"ok": True, **dict(self.result)}
        try:
            result = self.refresh()
        except Exception as exc:
            with self._lock:
                if self.result is None:
                    self.result = {"adb": [], "ssh": [],
                                   "generated_at": now_ms()}
                result = dict(self.result, error=str(exc))
        return {"ok": True, **dict(result)}


def _import_discovered(host, payload):
    """Handle import discovered."""
    items = payload.get("items") or []
    created, skipped = [], []
    existing = host.store.list()
    for item in items:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "").strip().lower()
        address = str(item.get("host") or "").strip()
        if typ not in ("adb", "ssh") or not address:
            skipped.append({"type": typ, "host": address,
                            "error": "type/host 无效"})
            continue
        if any(d.get("type") == typ and d.get("host") == address
               for d in existing):
            skipped.append({"type": typ, "host": address,
                            "error": "设备已存在"})
            continue
        name = str(item.get("name") or "").strip() or (
            "adb-%s" % address if typ == "adb" else "ssh-%s" % address)
        dev = {"type": typ, "host": address, "name": name,
               "remark": str(item.get("remark") or "自动发现").strip()}
        if typ == "ssh":
            try:
                port = int(item.get("port") or 22)
            except (TypeError, ValueError):
                port = 22
            dev.update({
                "port": port,
                "user": str(item.get("user") or "root").strip(),
                "auth": item.get("auth") or "key",
            })
        try:
            resp, _status = host.devices_add(dev)
        except Exception as exc:
            resp = {"ok": False, "error": str(exc)}
        if not resp.get("ok"):
            skipped.append({"type": typ, "host": address,
                            "error": resp.get("error") or "添加失败"})
            continue
        device = dict(resp["device"])
        try:
            check = host.device_check(device["id"])
            if isinstance(check, tuple):
                check = check[0]
        except Exception as exc:
            check = {"ok": False, "error": str(exc)}
        device["check"] = check
        created.append(device)
        existing.append(device)
    return {"ok": True, "devices": created, "skipped": skipped}


class Handler(BaseHTTPRequestHandler):
    server_version = "rkss-portal/" + VERSION

    def log_message(self, fmt, *args):
        if getattr(self.server, "access_log", False):
            sys.stderr.write("[portal] %s %s\n" %
                             (self.client_address[0], fmt % args))

    def send_response(self, code, message=None):
        METRICS.observe_http_status(code)
        return super().send_response(code, message)

    def _send(self, code, payload, ctype="application/json; charset=utf-8",
              headers=None):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        elif isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers or ():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = path[len("/static/"):]
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        static_root = os.path.realpath(STATIC_DIR)
        try:
            inside = os.path.commonpath((static_root, target)) == static_root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(target):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(target)[1]
        ctype = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml"}.get(
            ext, "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _check_request_body_length(self, path):
        # Raw file upload has its own streaming 2 GiB limit. Every other body
        # is a control request, even if Content-Type is misleading.
        if path.startswith("/api/fs/") and path.endswith("/upload"):
            if self.headers.get("Transfer-Encoding"):
                self._send(400, {"ok": False,
                                 "error": "chunked uploads are not supported"})
                return False
            raw = self.headers.get("Content-Length")
            try:
                length = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                self._send(400, {"ok": False,
                                 "error": "invalid Content-Length"})
                return False
            if length <= 0:
                self._send(400, {"ok": False, "error": "空 body"})
                return False
            if length > 2 << 30:
                self._send(413, {"ok": False, "error": "单文件超过 2GB"})
                return False
            return True
        if self.headers.get("Transfer-Encoding"):
            self._send(400, {"ok": False, "error":
                             "chunked request bodies are not supported"})
            return False
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            self._send(400, {"ok": False,
                             "error": "invalid Content-Length"})
            return False
        if length < 0:
            self._send(400, {"ok": False,
                             "error": "invalid Content-Length"})
            return False
        if length > MAX_JSON_BODY:
            self._send(413, {"ok": False,
                             "error": "request body exceeds 1 MiB limit"})
            return False
        return True

    def _require_auth(self):
        if self.server.auth.verify_headers(self.headers):
            return True
        self._send(401, {"ok": False, "code": "unauthorized",
                         "error": "authentication required"}, headers=(
            ("WWW-Authenticate", 'Bearer realm="rkss-portal"'),))
        return False

    def _auth_status(self):
        auth = self.server.auth
        return self._send(200, {
            "ok": True,
            "enabled": auth.enabled,
            "authenticated": auth.verify_headers(self.headers),
        })

    def _auth_login(self):
        auth = self.server.auth
        if not auth.enabled:
            return self._send(200, {"ok": True, "enabled": False,
                                    "authenticated": True})
        client_ip = self.client_address[0] if self.client_address else ""
        if not auth.login_allowed(client_ip):
            return self._send(429, {"ok": False, "code": "login_rate_limited",
                                    "error": "too many login failures"}, headers=(
                ("Retry-After", "300"),))
        body = self._json_body()
        token = body.get("token") if isinstance(body, dict) else None
        if not auth.verify_token(token):
            auth.record_login(client_ip, False)
            return self._send(401, {"ok": False, "code": "unauthorized",
                                    "error": "invalid token"})
        auth.record_login(client_ip, True)
        return self._send(200, {"ok": True, "enabled": True,
                                "authenticated": True}, headers=(
            ("Set-Cookie", auth.login_cookie()),))

    def _auth_logout(self):
        return self._send(200, {"ok": True}, headers=(
            ("Set-Cookie", self.server.auth.logout_cookie()),))

    def _same_origin_cookie_request(self):
        """Reject cross-origin state changes that authenticate via a cookie."""
        if not self.server.auth.enabled or not self.headers.get("Cookie"):
            return True
        authorization = self.headers.get("Authorization") or ""
        if authorization.lower().startswith("bearer "):
            return True
        origin = self.headers.get("Origin") or ""
        if not origin:
            self._send(403, {"ok": False, "code": "origin_required",
                             "error": "Origin header required for cookie authentication"})
            return False
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host"):
            self._send(403, {"ok": False, "code": "origin_mismatch",
                             "error": "cross-origin request rejected"})
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/" or path == "/index.html":
            return self._send(200, self.server.index_html,
                              "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._static(path)
        if path == "/api/health":
            return self._send(200, {"ok": True, "service": "rkss-portal",
                                    "version": VERSION})
        if path == "/api/auth/status":
            return self._auth_status()
        if not self._require_auth():
            return
        if path == "/api/metrics":
            return self._send(200, cached_runtime_metrics_snapshot(self.server))
        if path == "/api/discover":
            force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
            return self._send(200, self.server.discover.get(force=force))
        if path == "/api/discover/rules":
            return self._send(200, {"ok": True,
                                    "rules": self.server.rules.get()})
        if host_api_dispatch(self, self.server.host, "GET", path, query):
            return
        if router_dispatch(self, self.server.host, "GET", path, query):
            return
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if not self._check_request_body_length(path):
            return
        if path == "/api/auth/login":
            return self._auth_login()
        if path == "/api/auth/logout":
            if not self._same_origin_cookie_request():
                return
            return self._auth_logout()
        if not self._require_auth():
            return
        if not self._same_origin_cookie_request():
            return
        if url.path == "/api/discover/import":
            return self._send(200, _import_discovered(
                self.server.host, self._json_body()))
        if host_api_dispatch(self, self.server.host, "POST", path, query):
            return
        if router_dispatch(self, self.server.host, "POST", path, query):
            return
        return self._send(404, {"ok": False, "error": "not found"})

    def do_PUT(self):
        url = urlparse(self.path)
        if not self._check_request_body_length(url.path):
            return
        if not self._require_auth():
            return
        if not self._same_origin_cookie_request():
            return
        if url.path == "/api/discover/rules":
            try:
                rules = self.server.rules.set(
                    self._json_body().get("rules"))
            except (ValueError, TypeError) as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            return self._send(200, {"ok": True, "rules": rules})
        if host_api_dispatch(self, self.server.host, "PUT", url.path, {}):
            return
        if router_dispatch(self, self.server.host, "PUT", url.path, {}):
            return
        return self._send(404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        url = urlparse(self.path)
        if not self._require_auth():
            return
        if not self._same_origin_cookie_request():
            return
        if host_api_dispatch(self, self.server.host, "DELETE", url.path, {}):
            return
        if router_dispatch(self, self.server.host, "DELETE", url.path, {}):
            return
        return self._send(404, {"ok": False, "error": "not found"})


def _is_loopback_bind(value):
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _close_host_for_exit(host):
    """Close lifecycle services and translate an incomplete drain to exit 1."""
    try:
        close_ok = host.close()
    except Exception as exc:
        sys.stderr.write("[portal] ERROR: lifecycle shutdown failed: %s\n" % exc)
        return 1
    if close_ok is False:
        sys.stderr.write(
            "[portal] ERROR: lifecycle/audit shutdown did not drain within "
            "the deadline\n")
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rkss-portal")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--sim", action="store_true",
                    help="注册一个 local 模拟设备（供演示）")
    ap.add_argument("--conf-dir",
                    default=os.path.join(os.path.expanduser("~"), ".rkss"),
                    help="上位机配置/设备/临时文件目录（默认 ~/.rkss）")
    ap.add_argument("--max-http-workers", type=int,
                    default=int(os.environ.get("RKSS_HTTP_MAX_WORKERS", "64")),
                    help="HTTP 并发上限（默认 64）")
    ap.add_argument("--http-idle-timeout", type=float, default=30.0,
                    help="HTTP socket 空闲超时秒数（默认 30）")
    ap.add_argument("--access-log", action="store_true",
                    help="输出逐请求访问日志（高负载时默认关闭）")
    auth_group = ap.add_mutually_exclusive_group()
    auth_group.add_argument(
        "--auth-token-file",
        default=os.environ.get("RKSS_AUTH_TOKEN_FILE"),
        help="单管理员 token 文件（必须为 0600）")
    auth_group.add_argument(
        "--no-auth", action="store_true",
        help="显式关闭认证（非 loopback 暴露时不安全）")
    ap.add_argument(
        "--external-https", action="store_true",
        help="portal 位于可信 HTTPS 反代后；为登录 cookie 添加 Secure")
    ap.add_argument(
        "--trusted-http", action="store_true",
        help="允许直接非 loopback HTTP（仅限已有加密的可信网络，危险）")
    args = ap.parse_args(argv)

    if args.max_http_workers < 1:
        ap.error("--max-http-workers must be >= 1")
    if args.http_idle_timeout <= 0:
        ap.error("--http-idle-timeout must be > 0")
    loopback = _is_loopback_bind(args.bind)
    if not loopback and not args.trusted_http:
        ap.error("direct non-loopback HTTP is disabled; bind to loopback behind "
                 "an HTTPS proxy, or explicitly use --trusted-http on an "
                 "already encrypted trusted network")
    if not loopback and args.trusted_http:
        sys.stderr.write(
            "[portal] WARNING: trusted-http exposes plain HTTP on a non-loopback "
            "interface; use only inside an already encrypted trusted network\n")
    if args.external_https and args.trusted_http:
        ap.error("--external-https cannot be combined with --trusted-http")
    if args.no_auth:
        auth = TokenAuth.disabled(secure_cookie=args.external_https)
        sys.stderr.write("[portal] WARNING: authentication explicitly disabled\n")
    elif args.auth_token_file:
        try:
            auth = TokenAuth.from_file(
                args.auth_token_file, secure_cookie=args.external_https)
        except AuthConfigError as exc:
            ap.error(str(exc))
    elif loopback:
        auth = TokenAuth.disabled(secure_cookie=args.external_https)
    else:
        ap.error("non-loopback bind requires --auth-token-file; "
                 "use --no-auth only for a trusted network")

    host = HostApi(args.conf_dir, sim=args.sim)
    rules_store = RulesStore(args.conf_dir)
    discover_cache = DiscoverCache(host, rules_store)
    index_html = "not found"
    for cand in (os.path.join(STATIC_DIR, "index.html"),
                 os.path.join(BASE_DIR, "static", "index.html")):
        if os.path.isfile(cand):
            with open(cand, encoding="utf-8") as fh:
                index_html = fh.read()
            break

    srv = BoundedThreadingHTTPServer(
        (args.bind, args.port), Handler,
        max_workers=args.max_http_workers,
        idle_timeout=args.http_idle_timeout)
    srv.index_html = index_html
    srv.host = host
    srv.discover = discover_cache
    srv.rules = rules_store
    srv.auth = auth
    srv.access_log = args.access_log
    load_handlers()
    print("[portal] listen=%s:%d discovery=on auth=%s workers=%d "
          "access_log=%s conf=%s" % (
        args.bind, args.port, "on" if auth.enabled else "off",
        args.max_http_workers, "on" if args.access_log else "off",
        args.conf_dir), flush=True)
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def request_shutdown(_signum, _frame):
            # BaseServer.shutdown must run outside serve_forever's thread.
            threading.Thread(target=srv.shutdown,
                             name="rkss-sigterm-shutdown", daemon=True).start()

        signal.signal(signal.SIGTERM, request_shutdown)
    exit_code = 0
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            discover_cache.stop()
        except Exception as exc:
            sys.stderr.write("[portal] ERROR: discovery shutdown failed: %s\n" %
                             exc)
            exit_code = 1
        try:
            srv.server_close()
            if not srv.drain(timeout=5.0):
                sys.stderr.write("[portal] ERROR: HTTP drain timed out\n")
                exit_code = 1
        except Exception as exc:
            sys.stderr.write("[portal] ERROR: HTTP shutdown failed: %s\n" %
                             exc)
            exit_code = 1
        exit_code = max(exit_code, _close_host_for_exit(host))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
