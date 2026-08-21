"""Utilities for host.api.handlers.periph."""
from host.api.router import register
from host.core.http import send_json
from host.service import peripherals as periph_svc


def _transport(host, did, handler):
    """Handle transport."""
    try:
        return host._transport(did)
    except KeyError:
        send_json(handler, 404, {"ok": False, "error": "设备不存在: %s" % did})
    except Exception as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
    return None


def _periph_get(handler, host, match, query, fn):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    try:
        data = fn(t, device_id=match.group(1))
    except periph_svc.PeriphError as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, data)


def periph_i2c(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.i2c)


def periph_gpio(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.gpio)


def periph_pwm(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.pwm)


def periph_spi(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.spi)


def periph_uart(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.uart)


def periph_clk(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.clk)


def periph_watchdog(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.watchdog)


def periph_regulator(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.regulator)


def periph_dma(handler, host, match, query):
    return _periph_get(handler, host, match, query, periph_svc.dma)


register("GET", r"^/api/periph/([^/]+)/i2c$", periph_i2c, "periph.i2c")
register("GET", r"^/api/periph/([^/]+)/gpio$", periph_gpio, "periph.gpio")
register("GET", r"^/api/periph/([^/]+)/pwm$", periph_pwm, "periph.pwm")
register("GET", r"^/api/periph/([^/]+)/spi$", periph_spi, "periph.spi")
register("GET", r"^/api/periph/([^/]+)/uart$", periph_uart, "periph.uart")
register("GET", r"^/api/periph/([^/]+)/clk$", periph_clk, "periph.clk")
register("GET", r"^/api/periph/([^/]+)/watchdog$", periph_watchdog, "periph.watchdog")
register("GET", r"^/api/periph/([^/]+)/regulator$", periph_regulator, "periph.regulator")
register("GET", r"^/api/periph/([^/]+)/dma$", periph_dma, "periph.dma")
