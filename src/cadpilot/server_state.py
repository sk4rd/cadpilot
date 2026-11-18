from dataclasses import dataclass

from .freecad_client import FreeCADConnection


@dataclass
class ServerState:
    only_text_feedback: bool = False
    rpc_host: str = "localhost"
    rpc_port: int = 9876
    auto_audit: bool = True
    freecad_connection: FreeCADConnection | None = None
