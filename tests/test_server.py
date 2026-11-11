"""Tests for ``fleetnotes serve`` (fleetnotes.server): the overlay and its two
JSON files over HTTP, and nothing else.

A real server on 127.0.0.1 (a free port) serves a temp output folder that
also holds private-looking decoys (rules, salt, config): none may be served.
Requests go through http.client, so paths such as ``/../x`` reach the server
exactly as written.

Run from the repository root:
    python -m unittest discover -s tests -t . -v
"""

import http.client
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleetnotes.server import OVERLAY_FILENAME, make_server, overlay_path

OVERLAY = b"<!DOCTYPE html><title>test overlay</title><p>kumquat-overlay</p>\n"
NOTES = b'{"schema": "fleetnotes-notes.v1", "off": false, "notes": []}'
DECOYS = {"rules.conf": b'project lighthouse "Lighthouse"\n', "salt": b"0011223344556677\n",
          "config.toml": b'firstmate_home = "/opt/fm/main"\n', "x": b"pomelo-decoy\n",
          "fleetnotes.lock": b""}


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        cls.output = os.path.join(root, "public")
        os.makedirs(cls.output)
        cls.overlay = os.path.join(root, "overlay", "custom.html")
        os.makedirs(os.path.dirname(cls.overlay))
        with open(cls.overlay, "wb") as f:
            f.write(OVERLAY)
        with open(os.path.join(cls.output, "notes.json"), "wb") as f:
            f.write(NOTES)
        for name, body in DECOYS.items():  # in the served folder and next to it
            for folder in (cls.output, root):
                with open(os.path.join(folder, name), "wb") as f:
                    f.write(body)
        cls.server = make_server("127.0.0.1", 0, cls.output, overlay=cls.overlay)
        serve(cls.server)
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, host="127.0.0.1", port=None):
        conn = http.client.HTTPConnection(host, port or self.port, timeout=10)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def test_the_overlay_at_the_root_and_by_name(self):
        for path in ("/", f"/{OVERLAY_FILENAME}", "/?speed=120", f"/{OVERLAY_FILENAME}?speed=240"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, OVERLAY)
                self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["content-length"], str(len(OVERLAY)))

    def test_the_notes_file_is_served_as_is(self):
        for path in ("/notes.json", "/notes.json?t=1", "/notes.json?t=1#x"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, NOTES)
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["cache-control"], "no-store")

    def test_the_feed_is_served_only_while_it_exists(self):
        self.assertEqual(self.request("GET", "/fleet.json")[0], 404)
        feed = os.path.join(self.output, "fleet.json")
        with open(feed, "wb") as f:
            f.write(b'{"schema": "fleetnotes-feed.v1"}')
        try:
            status, headers, body = self.request("GET", "/fleet.json?t=2")
            self.assertEqual((status, body), (200, b'{"schema": "fleetnotes-feed.v1"}'))
            self.assertEqual(headers["content-type"], "application/json")
        finally:
            os.remove(feed)
        self.assertEqual(self.request("GET", "/fleet.json")[0], 404)  # read on every request

    def test_nothing_else_is_served(self):
        for path in ("/rules.conf", "/../x", "/notes.json/..", "/salt", "/config.toml", "/x",
                     "/fleetnotes.lock", "/public/notes.json", "/notes.json/", "//salt", "///rules.conf",
                     "/NOTES.JSON", "/%6Eotes.json", "/./notes.json", "/fleetnotes.html/..",
                     "/index.html", "/overlay/custom.html", "/custom.html", "/..%2Fx", "*"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 404)
                for decoy in DECOYS.values():
                    if decoy:
                        self.assertNotIn(decoy.strip(), body)
                self.assertNotIn(b"kumquat", body)

    def test_a_doubled_leading_slash_is_the_same_public_file_or_nothing(self):
        # Recent Pythons' http.server collapse a leading "//" to "/" before the
        # route lookup; older ones leave it (no route). Either way only the public file.
        status, _, body = self.request("GET", "//notes.json")
        self.assertIn(status, (200, 404))
        if status == 200:
            self.assertEqual(body, NOTES)

    def test_head_answers_without_a_body(self):
        status, headers, body = self.request("HEAD", "/notes.json")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-length"], str(len(NOTES)))
        self.assertEqual(self.request("HEAD", "/salt")[0], 404)

    def test_other_methods_are_refused(self):
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, "/notes.json")[0], 501)

    def test_the_server_header_names_no_python_version(self):
        headers = self.request("GET", "/")[1]
        self.assertEqual(headers["server"].strip(), "fleetnotes")
        self.assertNotIn("Python", headers["server"])


class PackagedOverlayTest(unittest.TestCase):
    def test_the_packaged_overlay_exists(self):
        path = overlay_path()
        self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(os.path.basename(path), OVERLAY_FILENAME)
        with open(path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("fleetnotes-notes.v1", page)
        self.assertIn("fleetnotes-feed.v1", page)
        self.assertIn("readJson('notes.json')", page)
        self.assertIn("readJson('fleet.json')", page)

    def test_the_default_server_serves_the_packaged_overlay(self):
        with tempfile.TemporaryDirectory() as output:
            server = make_server("127.0.0.1", 0, output)
            serve(server)
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/")
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
        with open(overlay_path(), "rb") as f:
            self.assertEqual((resp.status, body), (200, f.read()))

    def test_an_ipv6_loopback_host(self):
        with tempfile.TemporaryDirectory() as output:
            with open(os.path.join(output, "notes.json"), "wb") as f:
                f.write(NOTES)
            try:
                server = make_server("::1", 0, output)
            except OSError as e:
                self.skipTest(f"no IPv6 loopback here ({e.__class__.__name__})")
            self.assertEqual(server.address_family, socket.AF_INET6)
            serve(server)
            try:
                conn = http.client.HTTPConnection("::1", server.server_address[1], timeout=10)
                conn.request("GET", "/notes.json")
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual((resp.status, body), (200, NOTES))


if __name__ == "__main__":
    unittest.main()
