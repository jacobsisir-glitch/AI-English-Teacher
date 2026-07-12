import asyncio
import ssl
from urllib.parse import urlparse

import websockets


def build_ssl_context(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "wss":
        return None
    hostname = parsed.hostname or ""
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        ssl_context = ssl._create_unverified_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context
    return ssl.create_default_context()


async def probe(url: str) -> None:
    parsed = urlparse(url)
    print(f"[CHECK] url={url} scheme={parsed.scheme}")
    try:
        connect_kwargs = {
            "max_size": None,
            "ping_interval": 20,
            "ping_timeout": 20,
            "subprotocols": ["binary"],
        }
        ssl_context = build_ssl_context(url)
        if parsed.scheme == "wss":
            connect_kwargs["ssl"] = ssl_context
        ws = await websockets.connect(url, **connect_kwargs)
        print(f"[OK] connected {url} subprotocol={ws.subprotocol!r}")
        await ws.close()
    except Exception as exc:
        print(f"[FAIL] {url}")
        print(f"       type={type(exc).__name__}")
        print(f"       repr={repr(exc)}")


async def main() -> None:
    await probe("ws://127.0.0.1:10095")
    await probe("wss://127.0.0.1:10095")


if __name__ == "__main__":
    asyncio.run(main())
