"""Utilities for host.api.router."""
import importlib
import pkgutil
import re

ROUTES = []  # (method, compiled_regex, handler_ref, capability)


def register(method, pattern, handler, capability=""):
    """Handle register."""
    ROUTES.append((method, re.compile(pattern), handler, capability))


def dispatch(handler, host, method, path, query):
    for m, regex, handler_ref, capability in ROUTES:
        if m == method:
            mm = regex.match(path)
            if mm:
                if handler_ref(handler, host, mm, query):
                    return True
    return False


def load_handlers():
    """Return load handlers."""
    pkg = importlib.import_module("host.api.handlers")
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name != "legacy":
            importlib.import_module("host.api.handlers." + m.name)


class RouteTable:
    """Manage route table."""

    @staticmethod
    def register(method, pattern, handler, capability=""):
        register(method, pattern, handler, capability)

    @staticmethod
    def dispatch(handler, host, method, path, query):
        return dispatch(handler, host, method, path, query)
