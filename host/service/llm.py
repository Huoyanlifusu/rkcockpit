"""Utilities for host.service.llm."""
import json
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit


_TOKEN_HINTS = ("context", "token")



MAX_LLM_CONCURRENCY = 2
LLM_CONCURRENCY_SEM = threading.Semaphore(MAX_LLM_CONCURRENCY)


def _mask_url(url):
    """Handle mask url."""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]
        return "%s://%s%s" % (parts.scheme, netloc, parts.path or "")
    except Exception:
        return "<llm-url>"


class LLMNotConfigured(Exception):
    """Manage llmnot configured."""


class LLMUnreachable(Exception):
    """Manage llmunreachable."""


class LLMTimeout(Exception):
    """Manage llmtimeout."""


class LLMAuth(Exception):
    """Manage llmauth."""


class LLMRateLimit(Exception):
    """Manage llmrate limit."""


class LLMTokenLimit(Exception):
    """Manage llmtoken limit."""


def _raise_for_status(status, text, url):
    """Handle raise for status."""
    if status in (401, 403):
        raise LLMAuth("LLM 鉴权失败（HTTP %s），请检查 api_key" % status)
    if status == 429:
        raise LLMRateLimit("LLM 限流（HTTP 429），请稍后重试")
    if status == 400:
        low = text.lower()
        if any(k in low for k in _TOKEN_HINTS):
            raise LLMTokenLimit(
                "请求超出模型上下文/token 上限，请精简历史或任务")
        raise LLMUnreachable("LLM 请求被拒（HTTP 400）: %s"
                             % text[:200])
    if status >= 500:
        raise LLMUnreachable("LLM 服务错误（HTTP %s）: %s"
                             % (status, text[:200]))
    if status != 200:
        raise LLMUnreachable("LLM 响应异常（HTTP %s）" % status)


def estimate_tokens(messages):
    """Handle estimate tokens."""
    total = 0
    for m in messages or []:
        try:
            total += max(1, len(json.dumps(m)) // 4)
        except (TypeError, ValueError):
            total += 1
    return total


class LLMClient:

    def __init__(self, base_url="", api_key="", model="", timeout=30,
                 max_tokens=1024, _send=None, _stream_open=None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.max_tokens = max_tokens


        self._send = _send or self._default_send



        self._stream_open = _stream_open or self._default_stream_open

    @property
    def configured(self):
        return bool(self.base_url and self.model)



    def _default_send(self, method, url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()

    def _default_stream_open(self, method, url, headers, body, timeout):
        """Handle default stream open."""
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp



    def complete(self, messages, tools=None, temperature=0.0,
                 max_tokens=None, extra=None):
        """Handle complete."""
        if not self.configured:
            raise LLMNotConfigured(
                "LLM 未配置：请在配置目录 llm.json 中填写 base_url 与 model")
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature}
        if tools is not None:
            payload["tools"] = tools
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        elif self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if extra:
            payload.update(extra)
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            status, raw = self._send("POST", url, headers, body, self.timeout)
        except LLMTimeout:
            raise
        except (socket.timeout, TimeoutError):
            raise LLMTimeout("LLM 请求超时（%ss）：%s"
                             % (self.timeout, _mask_url(url)))
        except urllib.error.HTTPError as exc:


            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMTimeout("LLM 请求超时（%ss）：%s"
                                 % (self.timeout, _mask_url(url)))
            raise LLMUnreachable("LLM 不可达: %s" % (reason or exc))
        except Exception as exc:
            raise LLMUnreachable("LLM 请求失败: %s" % (str(exc)[:200]))

        text = raw.decode("utf-8", errors="replace")\
            if isinstance(raw, (bytes, bytearray)) else str(raw)


        _raise_for_status(status, text, url)

        try:
            data = json.loads(text)
        except ValueError:
            raise LLMUnreachable("LLM 响应不是合法 JSON")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMUnreachable("LLM 响应缺少 choices")

        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append({
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
            })
        return {
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": choices[0].get("finish_reason"),
            "usage": data.get("usage"),
        }



    def complete_stream(self, messages, tools=None, temperature=0.0,
                        max_tokens=None, extra=None):
        """Handle complete stream."""
        if not self.configured:
            raise LLMNotConfigured(
                "LLM 未配置：请在配置目录 llm.json 中填写 base_url 与 model")
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "stream": True}
        if tools is not None:
            payload["tools"] = tools
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        elif self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if extra:
            payload.update(extra)
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            status, resp = self._stream_open("POST", url, headers, body,
                                             self.timeout)
        except LLMTimeout:
            raise
        except (socket.timeout, TimeoutError):
            raise LLMTimeout("LLM 请求超时（%ss）：%s"
                             % (self.timeout, _mask_url(url)))
        except urllib.error.HTTPError as exc:

            try:
                raw = exc.read()
            except Exception:
                raw = b""
            text = raw.decode("utf-8", errors="replace")\
                if isinstance(raw, (bytes, bytearray)) else str(raw)
            _raise_for_status(exc.code, text, url)
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMTimeout("LLM 请求超时（%ss）：%s"
                                 % (self.timeout, _mask_url(url)))
            raise LLMUnreachable("LLM 不可达: %s" % (reason or exc))
        except Exception as exc:
            raise LLMUnreachable("LLM 请求失败: %s" % (str(exc)[:200]))

        if status != 200:
            try:
                raw = resp.read()
            except Exception:
                raw = b""
            text = raw.decode("utf-8", errors="replace")\
                if isinstance(raw, (bytes, bytearray)) else str(raw)
            _raise_for_status(status, text, url)

        yield from self._iter_stream(resp)

    def _iter_stream(self, resp):
        """Handle iter stream."""
        tool_acc = {}
        tool_order = []
        usage = None
        try:
            while True:
                try:
                    line = resp.readline()
                except (socket.timeout, TimeoutError):
                    raise LLMTimeout("LLM 流式读取超时")
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
                choices = ev.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield {"type": "token", "text": text}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    acc = tool_acc.get(idx)
                    if acc is None:
                        acc = {"id": "", "name": "", "arguments": ""}
                        tool_acc[idx] = acc
                        tool_order.append(idx)
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["name"] = fn["name"]
                    if fn.get("arguments"):
                        acc["arguments"] += fn["arguments"]
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if tool_order:
            yield {"type": "tool_calls", "tool_calls": [
                {"id": tool_acc[i]["id"], "name": tool_acc[i]["name"],
                 "arguments": tool_acc[i]["arguments"]} for i in tool_order]}
        yield {"type": "done", "usage": usage}
