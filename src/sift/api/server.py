"""Running the API with uvicorn."""

from __future__ import annotations

import threading
import webbrowser

import uvicorn

from sift.api.app import STATIC_DIR, create_app
from sift.core.config import LOOPBACK_HOSTS
from sift.core.service import SiftService


def run_server(service: SiftService, host: str, port: int, *, open_browser: bool = False) -> None:
    """Serve the dashboard and API until interrupted. Loopback addresses only."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError("the dashboard only binds to loopback addresses")
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{port}"
    print(f"Sift dashboard: {url}")
    if service.roots:
        print("Scan roots:")
        for root in service.roots:
            print(f"  {root}")
    else:
        print("No scan roots configured - nothing can be scanned from the dashboard.")
        print("Add roots to config.toml or restart with: sift serve --root PATH")
    if not (STATIC_DIR / "index.html").is_file():
        print("The dashboard has not been built; only the API is available.")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(service), host=host, port=port, log_level="warning")
