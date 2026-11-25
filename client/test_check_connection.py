import io
import json
import unittest
from unittest.mock import patch

from check_connection import check_connection


class ConnectionCheckTests(unittest.TestCase):
    def probe(self, pages):
        self.calls = []
        protocol = "2025-11-25"

        def respond(request, timeout):
            message = json.loads(request.data)
            method = message["method"]
            if method == "initialize":
                result = {"protocolVersion": protocol}
            else:
                self.assertEqual(request.get_header("Mcp-session-id"), "test-session")
                self.assertEqual(request.get_header("Mcp-protocol-version"), protocol)
                if method == "notifications/initialized":
                    response = io.BytesIO()
                    response.status = 202
                    return response
                self.assertEqual(method, "tools/list")
                cursor = message["params"].get("cursor")
                self.assertNotIn(cursor, self.calls)
                self.calls.append(cursor)
                self.assertEqual(message["id"], 1 + len(self.calls))
                result = pages[cursor]
            reply = io.BytesIO(json.dumps({"id": message["id"], "result": result}).encode())
            reply.headers = {"Mcp-Session-Id": "test-session"}
            return reply

        with patch("check_connection.build_opener") as opener:
            opener.return_value.open.side_effect = respond
            return check_connection("https://example.test/mcp", "fixture", None)

    def test_single_page(self):
        self.assertEqual(self.probe({None: {"tools": [{"name": "register_agent"}]}}), ("2025-11-25", 1))
        self.assertEqual(self.calls, [None])

    def test_register_agent_on_second_page(self):
        self.assertEqual(self.probe({
            None: {"tools": [{"name": "list_channels"}], "nextCursor": "page-2"},
            "page-2": {"tools": [{"name": "register_agent"}], "nextCursor": "page-3"},
            "page-3": {"tools": [{"name": "read_events"}]},
        }), ("2025-11-25", 3))
        self.assertEqual(self.calls, [None, "page-2", "page-3"])

    def test_missing_registration_after_last_page(self):
        with self.assertRaisesRegex(ValueError, "register_agent"):
            self.probe({None: {"tools": [], "nextCursor": "last"}, "last": {"tools": []}})
        self.assertEqual(self.calls, [None, "last"])

    def test_invalid_or_repeated_cursor(self):
        for cursor in ([], 1, "loop"):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, "cursor"):
                self.probe({None: {"tools": [], "nextCursor": "loop"},
                            "loop": {"tools": [], "nextCursor": cursor}})
