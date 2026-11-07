"""极简 httpx 兼容层（仅用于离线测试真实网络路径）。"""
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request


class Timeout:
    def __init__(self, timeout=None, connect=None, **kw):
        self.timeout = timeout


class Response:
    def __init__(self, status, raw, headers=None):
        self.status_code = status
        self.content = raw or b""
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class AsyncClient:
    def __init__(self, timeout=None, headers=None, follow_redirects=True, **kw):
        self.timeout = timeout
        self.headers = headers or {}
        self.is_closed = False

    async def get(self, url, params=None, **kw):
        headers = dict(self.headers or {})
        headers.update(kw.get("headers") or {})
        return await asyncio.to_thread(_fetch, url, params, headers)

    async def aclose(self):
        self.is_closed = True


def _fetch(url, params, headers):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return Response(r.status, r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read(), dict(e.headers or {}))


class Client:
    """同步客户端，供 tools/probe_api.py 之类的脚本使用。"""

    def __init__(self, timeout=None, headers=None, follow_redirects=True, **kw):
        self.timeout = timeout
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, **kw):
        headers = dict(self.headers or {})
        headers.update(kw.get("headers") or {})
        return _fetch(url, params, headers)

    def close(self):
        pass

