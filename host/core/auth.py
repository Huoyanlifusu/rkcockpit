"""Optional single-administrator token authentication."""
import hashlib
import hmac
import os
import stat
import threading
import time
import http.cookies


COOKIE_NAME = "rkss_auth"
_SESSION_CONTEXT = b"rkss-session-v1"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5


class AuthConfigError(ValueError):
    pass


class TokenAuth:
    def __init__(self, token=None, clock=None, secure_cookie=False):
        self._token = token
        self._clock = clock or time.time
        self._secure_cookie = bool(secure_cookie)
        self._login_lock = threading.Lock()
        self._login_failures = {}

    @classmethod
    def disabled(cls, secure_cookie=False):
        return cls(secure_cookie=secure_cookie)

    @classmethod
    def from_file(cls, path, secure_cookie=False):
        path = os.path.abspath(os.path.expanduser(path))
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise AuthConfigError("cannot read auth token file: %s" % exc)
        if not stat.S_ISREG(st.st_mode):
            raise AuthConfigError("auth token file is not a regular file")
        if st.st_nlink != 1:
            raise AuthConfigError("auth token file must have exactly one link")
        if st.st_uid != os.geteuid():
            raise AuthConfigError("auth token file must be owned by the portal user")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise AuthConfigError("auth token file permissions must be 0600")
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |\
                getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                    raise AuthConfigError("auth token file changed while opening")
                raw = os.read(fd, 4097)
            finally:
                os.close(fd)
            if len(raw) > 4096:
                raise AuthConfigError("auth token file is too large")
            token = raw.decode("utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise AuthConfigError("cannot read auth token file: %s" % exc)
        if not token:
            raise AuthConfigError("auth token file is empty")
        if "\n" in token or "\r" in token:
            raise AuthConfigError("auth token must be a single line")
        if len(token) < 32:
            raise AuthConfigError("auth token must contain at least 32 characters")
        return cls(token, secure_cookie=secure_cookie)

    @property
    def enabled(self):
        return self._token is not None

    def verify_token(self, candidate):
        return bool(self.enabled and isinstance(candidate, str) and
                    hmac.compare_digest(candidate, self._token))

    def verify_headers(self, headers):
        if not self.enabled:
            return True
        authorization = headers.get("Authorization") or ""
        scheme, sep, value = authorization.partition(" ")
        if sep and scheme.lower() == "bearer" and self.verify_token(value):
            return True
        raw_cookie = headers.get("Cookie") or ""
        try:
            cookies = http.cookies.SimpleCookie()
            cookies.load(raw_cookie)
            morsel = cookies.get(COOKIE_NAME)
            candidate = morsel.value if morsel else ""
        except Exception:
            candidate = ""
        return self.verify_session(candidate)

    def _session_mac(self, issued):
        message = _SESSION_CONTEXT + b":" + str(issued).encode("ascii")
        return hmac.new(self._token.encode("utf-8"), message,
                        hashlib.sha256).hexdigest()

    def verify_session(self, candidate):
        if not self.enabled or not isinstance(candidate, str):
            return False
        issued_raw, sep, supplied = candidate.partition(".")
        try:
            issued = int(issued_raw)
        except (TypeError, ValueError):
            return False
        now = int(self._clock())
        if not sep or issued > now + 60 or now - issued > SESSION_SECONDS:
            return False
        return hmac.compare_digest(supplied, self._session_mac(issued))

    def login_allowed(self, client_ip):
        now = self._clock()
        key = client_ip or "unknown"
        with self._login_lock:
            recent = [stamp for stamp in self._login_failures.get(key, ())
                      if now - stamp < LOGIN_WINDOW_SECONDS]
            self._login_failures[key] = recent
            return len(recent) < LOGIN_MAX_FAILURES

    def record_login(self, client_ip, success):
        key = client_ip or "unknown"
        with self._login_lock:
            if success:
                self._login_failures.pop(key, None)
            else:
                self._login_failures.setdefault(key, []).append(self._clock())

    def login_cookie(self):
        issued = int(self._clock())
        session = "%d.%s" % (issued, self._session_mac(issued))
        cookie = ("%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" %
                  (COOKIE_NAME, session, SESSION_SECONDS))
        return cookie + ("; Secure" if self._secure_cookie else "")

    def logout_cookie(self):
        cookie = ("%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" %
                  COOKIE_NAME)
        return cookie + ("; Secure" if self._secure_cookie else "")
