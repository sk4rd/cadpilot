"""The port a live script may use: a FreeCAD started for testing, never one someone works in."""

import os
import sys

# freecad-mcp's default and CADPilot's: the FreeCADs people model in
WORKING_PORTS = {"9875", "9876"}


def require_test_port() -> int:
    port = os.environ.get("CADPILOT_PORT", "").strip()
    if not port.isdigit() or port in WORKING_PORTS:
        sys.exit(
            "set CADPILOT_PORT to the port of a FreeCAD started for testing "
            "(scripts/live-freecad start <port>), never 9875 or 9876"
        )
    return int(port)
