"""Read-only v4 tree views and complete portable run exports."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from session_log import SessionStore
from v4_tree import TreeStore
from test_v4_tree import surface


class TreeApiTests(unittest.TestCase):
    def test_v3_has_no_tree_and_corrupt_v4_events_are_not_served_as_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            v3 = store.create("b" * 32, {"experiment_mode": "v3"})
            v3.close()
            v4 = store.create("c" * 32, {"experiment_mode": "v4", "target": "https://example.test/"})
            TreeStore(v4).initialize("https://example.test/")
            v4.close()
            with patch.object(app.EXPLORER, "store", store):
                client = TestClient(app.app, base_url="http://127.0.0.1:8765")
                self.assertEqual(client.get(f"/api/runs/{v3.run_id}/tree").status_code, 404)
                v4.events_path.write_text("{bad json}\n", encoding="utf-8")
                self.assertEqual(client.get(f"/api/runs/{v4.run_id}/tree").status_code, 503)

    def test_replays_tree_and_exports_node_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            log = store.create("a" * 32, {"experiment_mode": "v4", "target": "https://example.test/"})
            tree = TreeStore(log)
            root = tree.initialize("https://example.test/")
            child = tree.discover(root, surface("https://example.test/search"))
            log.close()
            with patch.object(app.EXPLORER, "store", store):
                client = TestClient(app.app, base_url="http://127.0.0.1:8765")
                response = client.get(f"/api/runs/{log.run_id}/tree")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["nodes"][child]["parent_id"], root)
                self.assertEqual(response.json()["root_id"], root)
                node = client.get(f"/api/runs/{log.run_id}/nodes/{child}")
                self.assertEqual(node.status_code, 200, node.text)
                self.assertEqual(node.json()["node"]["id"], child)
                exported = client.get(f"/api/runs/{log.run_id}/export")
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    self.assertIn("tree.json", archive.namelist())
                    self.assertIn(f"nodes/{child}.json", archive.namelist())


if __name__ == "__main__":
    unittest.main()
