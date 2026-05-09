"""
Start the gateway before your main app:
  pip install flask requests
  python proxy_gateway.py
Then point your app's proxy to http://127.0.0.1:8000
"""

from __future__ import annotations

import base64
import itertools
import os
import random
import select
import socket
import ssl
import threading
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, Response, request
from werkzeug.serving import WSGIRequestHandler, make_server

try:
    import socks  # type: ignore
except Exception:  # pragma: no cover - optional dependency for SOCKS
    socks = None

APP_HOST = "127.0.0.1"
APP_PORT = 8000
PROXIES_FILE = os.environ.get("PROXIES_FILE", "proxies.txt")
MAX_RETRIES = 3
TIMEOUT_SECONDS = 30
PROXY_SHUFFLE = os.environ.get("PROXY_SHUFFLE", "1") == "1"
HTTPS_ONLY = os.environ.get("HTTPS_ONLY", "0") == "1"
TARGET_ALLOWLIST = {
    host.strip().lower()
    for host in os.environ.get("TARGET_ALLOWLIST", "").split(",")
    if host.strip()
}

# Hop-by-hop headers per RFC 7230 (plus common proxy headers)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
}


def load_proxies(path: str) -> List[str]:
    if not os.path.exists(path):
        print(f"[proxy_gateway] proxies file not found: {path}")
        return []
    proxies: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(line)
    if not proxies:
        print(f"[proxy_gateway] proxies file is empty: {path}")
    elif PROXY_SHUFFLE:
        random.shuffle(proxies)
    return proxies


class ProxyPool:
    def __init__(self, proxies: Iterable[str]) -> None:
        self._proxies = list(proxies)
        self._lock = threading.Lock()
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None

    def next(self) -> Optional[str]:
        with self._lock:
            if not self._cycle:
                return None
            return next(self._cycle)

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)


def _filter_request_headers(in_headers: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in in_headers.items():
        lk = key.lower()
        if lk in HOP_BY_HOP_HEADERS or lk == "host":
            continue
        out[key] = value
    return out


def _filter_response_headers(in_headers: Dict[str, str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for key, value in in_headers.items():
        lk = key.lower()
        if lk in HOP_BY_HOP_HEADERS:
            continue
        out.append((key, value))
    return out


def _extract_target_url(req, path: str) -> Optional[str]:
    raw_uri = req.environ.get("RAW_URI") or req.environ.get("REQUEST_URI") or ""
    if raw_uri.startswith("http://") or raw_uri.startswith("https://"):
        return raw_uri

    if path.startswith("http://") or path.startswith("https://"):
        target = path
        if req.query_string:
            target += "?" + req.query_string.decode("utf-8", "ignore")
        return target

    host = req.headers.get("Host")
    if not host:
        return None
    scheme = req.headers.get("X-Forwarded-Proto", "http")
    target = f"{scheme}://{host}/{path}"
    if req.query_string:
        target += "?" + req.query_string.decode("utf-8", "ignore")
    return target


app = Flask(__name__)
proxy_pool = ProxyPool(load_proxies(PROXIES_FILE))


class ProxySpec:
    def __init__(
        self,
        scheme: str,
        host: str,
        port: int,
        username: Optional[str],
        password: Optional[str],
    ) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    @property
    def is_socks5(self) -> bool:
        return self.scheme.startswith("socks5")

    @property
    def rdns(self) -> bool:
        return self.scheme.endswith("h")


def _parse_proxy_url(proxy_url: str) -> ProxySpec:
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        raise ValueError(f"Invalid proxy URL: {proxy_url}")
    scheme = (parsed.scheme or "http").lower()
    if scheme.startswith("socks5"):
        default_port = 1080
    elif scheme == "https":
        default_port = 443
    else:
        default_port = 80
    port = parsed.port or default_port
    return ProxySpec(scheme, parsed.hostname, port, parsed.username, parsed.password)


def _redact_proxy_url(proxy_url: str) -> str:
    try:
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    except ValueError:
        return proxy_url
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username or parsed.password:
        userinfo = "***:***" if parsed.password is not None else "***"
        netloc = f"{userinfo}@{netloc}"
    return f"{parsed.scheme or 'http'}://{netloc}"


def _split_host_port(host: str) -> str:
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def _host_allowed(host: str) -> bool:
    if not TARGET_ALLOWLIST:
        return True
    norm = _split_host_port(host).strip(".").lower()
    for allowed in TARGET_ALLOWLIST:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if norm.endswith(suffix):
                return True
        elif allowed.startswith("."):
            if norm.endswith(allowed):
                return True
        else:
            if norm == allowed:
                return True
    return False


def _recv_until(sock: socket.socket, marker: bytes, max_bytes: int = 65536) -> bytes:
    data = b""
    while marker not in data and len(data) < max_bytes:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _http_connect_tunnel(proxy: ProxySpec, target_host: str, target_port: int) -> Tuple[Optional[socket.socket], int]:
    upstream = socket.create_connection((proxy.host, proxy.port), timeout=TIMEOUT_SECONDS)
    if proxy.scheme == "https":
        context = ssl.create_default_context()
        upstream = context.wrap_socket(upstream, server_hostname=proxy.host)

    connect_lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: keep-alive",
    ]
    if proxy.username:
        creds = f"{proxy.username}:{proxy.password or ''}".encode("utf-8")
        b64 = base64.b64encode(creds).decode("ascii")
        connect_lines.append(f"Proxy-Authorization: Basic {b64}")
    connect_data = "\r\n".join(connect_lines) + "\r\n\r\n"
    upstream.sendall(connect_data.encode("utf-8"))

    resp = _recv_until(upstream, b"\r\n\r\n")
    status_line = resp.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = status_line.split()
    status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 502
    if status_code != 200:
        upstream.close()
        return None, status_code
    return upstream, status_code


def _socks5_tunnel(proxy: ProxySpec, target_host: str, target_port: int) -> socket.socket:
    if socks is None:
        raise RuntimeError("SOCKS support requires requests[socks] (PySocks).")
    upstream = socks.socksocket()
    upstream.set_proxy(
        socks.SOCKS5,
        proxy.host,
        proxy.port,
        rdns=proxy.rdns,
        username=proxy.username,
        password=proxy.password,
    )
    upstream.settimeout(TIMEOUT_SECONDS)
    upstream.connect((target_host, target_port))
    return upstream


def _relay(client_sock: socket.socket, upstream_sock: socket.socket) -> None:
    client_sock.setblocking(False)
    upstream_sock.setblocking(False)
    sockets = [client_sock, upstream_sock]
    while True:
        readable, _, _ = select.select(sockets, [], [], TIMEOUT_SECONDS)
        if not readable:
            continue
        for sock in readable:
            try:
                data = sock.recv(8192)
            except OSError:
                return
            if not data:
                return
            other = upstream_sock if sock is client_sock else client_sock
            try:
                other.sendall(data)
            except OSError:
                return


class ProxyRequestHandler(WSGIRequestHandler):
    def do_CONNECT(self) -> None:  # noqa: N802
        if not proxy_pool.has_proxies:
            self.send_error(503, "No proxies available. Check proxies.txt")
            return

        if ":" not in self.path:
            self.send_error(400, "Invalid CONNECT target")
            return
        target_host, target_port_str = self.path.rsplit(":", 1)
        try:
            target_port = int(target_port_str)
        except ValueError:
            self.send_error(400, "Invalid CONNECT port")
            return
        if not _host_allowed(target_host):
            self.send_error(403, "CONNECT target not allowed")
            return

        last_error: Optional[str] = None
        upstream: Optional[socket.socket] = None
        for attempt in range(1, MAX_RETRIES + 1):
            proxy_url = proxy_pool.next()
            if not proxy_url:
                break
            try:
                proxy = _parse_proxy_url(proxy_url)
            except ValueError as exc:
                last_error = str(exc)
                continue

            try:
                if proxy.is_socks5:
                    upstream = _socks5_tunnel(proxy, target_host, target_port)
                    status_code = 200
                else:
                    upstream, status_code = _http_connect_tunnel(proxy, target_host, target_port)
                if status_code == 200 and upstream is not None:
                    break
                if status_code == 429:
                    print(
                        f"[proxy_gateway] node congestion via {_redact_proxy_url(proxy_url)} "
                        f"(attempt {attempt}/{MAX_RETRIES})"
                    )
                    last_error = "node congestion (429)"
                    continue
                last_error = f"CONNECT failed ({status_code})"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                upstream = None
                continue

        if upstream is None:
            msg = "Upstream CONNECT failed"
            if last_error:
                msg += f": {last_error}"
            self.send_error(502, msg)
            return

        try:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except OSError:
            upstream.close()
            return

        self.close_connection = True
        try:
            _relay(self.connection, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def proxy(path: str):
    if not proxy_pool.has_proxies:
        return Response("No proxies available. Check proxies.txt", status=503)

    target_url = _extract_target_url(request, path)
    if not target_url:
        return Response("Unable to determine target URL.", status=400)
    parsed_target = urlparse(target_url)
    if HTTPS_ONLY and parsed_target.scheme != "https":
        return Response("Plain HTTP is blocked by gateway policy.", status=403)
    if parsed_target.hostname and not _host_allowed(parsed_target.hostname):
        return Response("Target host not allowed by gateway policy.", status=403)

    data = request.get_data()
    headers = _filter_request_headers(dict(request.headers))

    last_error: Optional[str] = None
    for attempt in range(1, MAX_RETRIES + 1):
        proxy_url = proxy_pool.next()
        if not proxy_url:
            return Response("No proxies available.", status=503)

        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            upstream = requests.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=data,
                allow_redirects=False,
                stream=True,
                proxies=proxies,
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            continue

        if upstream.status_code == 429:
            print(
                f"[proxy_gateway] node congestion via {_redact_proxy_url(proxy_url)} "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )
            last_error = "node congestion (429)"
            continue

        upstream.raw.decode_content = False
        body = upstream.raw.read()
        resp_headers = _filter_response_headers(upstream.headers)
        return Response(body, status=upstream.status_code, headers=resp_headers)

    msg = f"Upstream failed after {MAX_RETRIES} attempts"
    if last_error:
        msg += f": {last_error}"
    return Response(msg, status=502)


if __name__ == "__main__":
    server = make_server(APP_HOST, APP_PORT, app, threaded=True, request_handler=ProxyRequestHandler)
    print(f"[proxy_gateway] listening on http://{APP_HOST}:{APP_PORT}")
    server.serve_forever()
