"""Start the optional web server without coupling it to heating control."""

import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


def start_web_server(host=None, port=8000):
    setting = os.environ.get("HEATAUTOMATION_WEB_AUTOSTART", "true").strip().lower()
    if setting in ("false", "0", "no", "off"):
        logger.info("Automatic web server startup is disabled.")
        return None
    if setting not in ("true", "1", "yes", "on"):
        logger.error("Invalid HEATAUTOMATION_WEB_AUTOSTART; web server not started.")
        return None
    if len(os.environ.get("HEATAUTOMATION_WEB_PASSWORD", "")) < 32:
        logger.warning(
            "Web server not started: set HEATAUTOMATION_WEB_PASSWORD to at least "
            "32 characters in heatautomation/.env or the environment."
        )
        return None
    host = host or os.environ.get("HEATAUTOMATION_WEB_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "Web server is binding to %s, not loopback; protect network access.", host
        )
    command = [
        sys.executable, "-m", "uvicorn", "heatautomation.web.app:create_app",
        "--factory", "--host", host, "--port", str(port),
    ]
    try:
        process = subprocess.Popen(command)
    except OSError:
        logger.exception("Could not start web server; heating control will continue.")
        return None
    stopping = threading.Event()

    def monitor():
        code = process.wait()
        if not stopping.is_set():
            logger.error(
                "Web server exited with code %s; heating control continues. "
                "Check the web server output, dependencies and port availability.", code
            )

    try:
        threading.Thread(target=monitor, name="web-server-monitor", daemon=True).start()
    except RuntimeError:
        logger.exception("Could not monitor web server; heating control will continue.")
        stop_web_server((process, stopping))
        return None
    logger.info("Starting web server at http://%s:%s; awaiting server readiness.", host, port)
    return process, stopping


def stop_web_server(server):
    if server is None:
        return
    process, stopping = server
    stopping.set()
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Web server did not stop in time; terminating it forcefully.")
            process.kill()
            process.wait()
    except OSError:
        logger.exception("Could not stop the web server process.")
