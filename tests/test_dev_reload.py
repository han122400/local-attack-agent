"""Exercise reload configuration without starting a server or browser."""

import contextlib
import io
import unittest
from unittest.mock import patch

import uvicorn
from uvicorn.supervisors.watchfilesreload import FileFilter

from engine.paths import PROJECT_ROOT
from web import app as web_app


class DevReloadTests(unittest.TestCase):
    def options(self, *arguments):
        with patch("sys.argv", ["app.py", *arguments]), patch("uvicorn.run") as run, contextlib.redirect_stdout(io.StringIO()):
            web_app.main()
        args, options = run.call_args
        self.assertEqual(args, ("web.app:app",))
        return options

    def test_default_launch_reloads_source_and_prompts_but_ignores_runtime_files(self):
        options = self.options()
        self.assertTrue(options["reload"])
        self.assertEqual(options["app_dir"], str(PROJECT_ROOT))
        config = uvicorn.Config("web.app:app", **{key: value for key, value in options.items() if key != "app_dir"})
        accepts = FileFilter(config)
        for relative in ("app.py", "orchestrator.py", "engine/tree.py", "web/app.py", "prompts/system/v4.txt"):
            with self.subTest(path=relative):
                self.assertTrue(accepts(PROJECT_ROOT / relative))
        for relative in ("runs/example/runtime/tools/EXEC-0001.py", "runs/example/session.json",
                         ".deps/playwright/example.py", "tests/test_app.py", "docs/example.md"):
            with self.subTest(path=relative):
                self.assertFalse(accepts(PROJECT_ROOT / relative))

    def test_no_reload_option_disables_watching_for_long_experiments(self):
        options = self.options("--no-reload")
        self.assertFalse(options["reload"])
        self.assertNotIn("reload_dirs", options)
