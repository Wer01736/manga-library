import os
from urllib.parse import urlparse


PROXY_ENVIRONMENT_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def sanitize_blocking_proxy_environment():
    """Remove only the known loopback port-9 blocker; preserve real proxies."""
    removed = []
    for name in PROXY_ENVIRONMENT_NAMES:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value)
            is_blocker = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port == 9
            )
        except ValueError:
            is_blocker = False
        if is_blocker:
            os.environ.pop(name, None)
            removed.append(name)
    return removed


if __name__ == "__main__":
    import json
    import sys
    import threading
    import time
    import urllib.error
    import urllib.request
    import webbrowser

    sanitize_blocking_proxy_environment()

    import uvicorn
    from app import app as web_app
    from comic_bridge import main as run_download_bridge

    def bridge_health():
        try:
            with urllib.request.urlopen("http://127.0.0.1:8766/api/18comic/health", timeout=0.8) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    expected_root = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
    existing_bridge = bridge_health()
    if existing_bridge:
        actual_root = os.path.normcase(os.path.abspath(existing_bridge.get("runtime_root") or ""))
        if actual_root != expected_root:
            raise RuntimeError(
                "8766 下載服務來自另一份漫畫網頁版。請先關閉攜帶版，再啟動主機版。"
            )
    else:
        bridge_thread = threading.Thread(
            target=run_download_bridge,
            name="comic-download-bridge",
            daemon=True,
        )
        bridge_thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            health = bridge_health()
            if health:
                actual_root = os.path.normcase(os.path.abspath(health.get("runtime_root") or ""))
                if actual_root == expected_root:
                    break
                raise RuntimeError("下載服務啟動位置不正確，已停止主機版啟動。")
            if not bridge_thread.is_alive():
                raise RuntimeError("下載服務啟動失敗；請檢查 8766 連接埠是否被其他程式使用。")
            time.sleep(0.1)
        else:
            raise RuntimeError("下載服務在 30 秒內沒有完成啟動。")

    port = int(os.environ.get("COMIC_WEB_PORT", "8765"))
    if getattr(sys, "frozen", False) and os.environ.get("COMIC_WEB_NO_BROWSER") != "1":
        threading.Timer(1.2, webbrowser.open, args=(f"http://127.0.0.1:{port}/",)).start()
    access_log = os.environ.get("COMIC_WEB_ACCESS_LOG") == "1"
    uvicorn.run(
        web_app,
        host="127.0.0.1",
        port=port,
        reload=False,
        access_log=access_log,
    )
