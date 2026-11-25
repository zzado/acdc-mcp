"""Check an ACDC MCP HTTPS endpoint using only the Python standard library."""

import argparse
import getpass
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_endpoint(value):
    try:
        url = urlsplit(value)
        _ = url.port
    except ValueError:
        raise argparse.ArgumentTypeError("Enter the full HTTPS MCP URL") from None
    if url.scheme != "https" or not url.hostname or url.username is not None or url.query or url.fragment:
        raise argparse.ArgumentTypeError("Enter the full HTTPS MCP URL without credentials or query parameters")
    return value


def check_connection(url, token, tls):
    opener = build_opener(RejectRedirects(), HTTPSHandler(context=tls))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
    }

    def request(method, params, request_id=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        payload = json.dumps(message).encode("utf-8")
        with opener.open(Request(url, data=payload, headers=headers), timeout=15) as response:
            if request_id is None:
                if response.status != 202:
                    raise ValueError("The server did not accept the MCP initialization notification")
                return None
            reply = json.load(response)
            if not isinstance(reply, dict) or reply.get("id") != request_id or "result" not in reply:
                raise ValueError(f"The server did not return an MCP result for {method}")
            session = response.headers.get("Mcp-Session-Id")
            if session:
                headers["Mcp-Session-Id"] = session
            return reply["result"]

    initialized = request("initialize", {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "acdc-mcp-connection-check", "version": "1.0"},
    }, 1)
    protocol = initialized.get("protocolVersion") if isinstance(initialized, dict) else None
    if protocol not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
        raise ValueError("The server did not negotiate a supported Streamable HTTP protocol version")
    headers["MCP-Protocol-Version"] = protocol
    request("notifications/initialized", {})
    catalog = request("tools/list", {}, 2)
    tools = catalog.get("tools") if isinstance(catalog, dict) else None
    if not isinstance(tools, list) or not any(isinstance(tool, dict) and tool.get("name") == "register_agent"
                                              for tool in tools):
        raise ValueError("The MCP tool list does not contain the ACDC MCP register_agent tool")
    return protocol, len(tools)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=https_endpoint, help="for example, https://mcp.example.org/mcp")
    parser.add_argument("--ca-file", help="PEM certificate file for a private CA")
    args = parser.parse_args()
    try:
        token = (os.getenv("TEAM_MCP_TOKEN") or getpass.getpass("Team MCP token: ")).strip()
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "Set TEAM_MCP_TOKEN before running the connection check.\n")
    if not token or not token.isascii() or any(not char.isprintable() or char.isspace() for char in token):
        parser.exit(1, "Enter a valid Agent token or set TEAM_MCP_TOKEN.\n")
    try:
        context = ssl.create_default_context()
        if args.ca_file:
            context.load_verify_locations(cafile=args.ca_file)
        protocol, count = check_connection(args.url, token, context)
    except HTTPError as error:
        hint = "Check the URL and server status."
        if error.code in {401, 403}:
            hint = "Check the Agent token and the exact MCP hostname."
        elif 300 <= error.code < 400:
            hint = "Use the final HTTPS MCP URL directly."
        parser.exit(1, f"HTTP {error.code}. {hint}\n")
    except (OSError, URLError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            parser.exit(1, "TLS certificate verification failed. Check the hostname and the trusted CA.\n")
        parser.exit(1, "HTTPS connection failed. Check DNS, server reachability and the CA file.\n")
    except json.JSONDecodeError:
        parser.exit(1, "Expected a JSON MCP response. Check the server URL.\n")
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print(f"Connected to ACDC MCP using {protocol}. Tools on the first page: {count}.")


if __name__ == "__main__":
    main()
