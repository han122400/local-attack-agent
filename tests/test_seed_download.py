import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from web import app as web_app


class SeedEvidenceDownloadTests(unittest.TestCase):
    def test_large_binary_artifact_is_downloaded_in_full_without_html_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = b"\x00<html>" + b"x" * 200000
            (root / "artifact.dat").write_bytes(data)
            with patch.object(web_app, "run_file_root", return_value=(root, True)):
                response = web_app.download_evidence("a" * 32, "artifact.dat")
            self.assertEqual(response.body, data)
            self.assertEqual(response.media_type, "application/octet-stream")
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_download_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(web_app, "run_file_root", return_value=(Path(folder), True)):
                with self.assertRaises(HTTPException):
                    web_app.download_evidence("a" * 32, "../secret")
