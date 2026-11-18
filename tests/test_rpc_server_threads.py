"""The addon's XML-RPC server answers status calls while another call blocks.

Live audit: with a single-threaded server, ping, get_gui_state and
get_addon_log all waited behind a running execute_code, and diagnose then told
the user to restart FreeCAD.
"""

import sys
import threading
import time
import xmlrpc.client
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot"
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from rpc_server import request_log  # noqa: E402


def test_ping_answers_while_another_call_is_blocked():
    release = threading.Event()
    server = request_log.LoggedXMLRPCServer(("127.0.0.1", 0), allow_none=True, logRequests=False)
    server.register_function(lambda: release.wait(10), "slow")
    server.register_function(lambda: True, "ping")
    port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    uri = f"http://127.0.0.1:{port}"
    slow = threading.Thread(target=lambda: xmlrpc.client.ServerProxy(uri).slow(), daemon=True)
    try:
        slow.start()
        time.sleep(0.2)
        started = time.monotonic()
        assert xmlrpc.client.ServerProxy(uri).ping() is True
        assert time.monotonic() - started < 2.0
        assert slow.is_alive(), "the slow call must still be running"
    finally:
        release.set()
        slow.join(timeout=5)
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)
