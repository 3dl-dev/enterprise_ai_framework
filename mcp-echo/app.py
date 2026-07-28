"""The trivial MCP server (enterpriseaiframework-12b).

Exists to prove the wiring ONCE: chat UI -> agents framework -> an MCP server we host ->
tool executes -> output reaches the reply. Everything else (web-search, skills,
code-execution) sits on top of this same path, so this server does the least possible
work and returns a value nothing else in the system could produce, which is what lets a
test tell "the model called the tool" apart from "the model guessed."

Speaks MCP over streamable-http (the transport LibreChat's MCPServersSchema validates
for `type: streamable-http`), so it can be registered under `mcpServers` in
librechat.yaml as a URL, deployed as its own Service in the cluster rather than a
subprocess LibreChat would have to spawn inside its own container.
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP(
    "enterprise-ai-echo",
    host="0.0.0.0",
    port=8080,
    # The chat pod calls this service by its cluster DNS name (mcp-echo:8080), which
    # is exactly what DNS-rebinding protection exists to reject on an
    # internet-facing server. This server is cluster-internal only, reachable from
    # nowhere else, so that protection has no attack surface here to protect.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back, prefixed with ECHO: .

    The prefix is the proof artifact: it cannot appear in a reply unless this
    function actually ran, so a test can assert on it to show the tool call
    reached the server and the output reached the model's reply, not merely that
    the model decided to call some tool.
    """
    return f"ECHO:{text}"


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-echo"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
