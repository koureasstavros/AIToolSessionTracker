import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.common import source_paths


class SourcePathTests(unittest.TestCase):
    def test_vscode_uses_macos_application_support(self) -> None:
        with patch.object(source_paths.sys, "platform", "darwin"), patch("src.common.source_paths._is_macos", return_value=True):
            with patch.object(Path, "home", return_value=Path("/Users/tester")):
                self.assertEqual(
                    source_paths.extension_roots()[0],
                    Path("/Users/tester/Library/Application Support/Code/User"),
                )

    def test_vscode_uses_xdg_config_on_linux(self) -> None:
        with patch.object(source_paths.sys, "platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/config"}):
            self.assertEqual(
                source_paths.extension_roots()[0],
                Path("/tmp/config/Code/User"),
            )

    def test_claude_desktop_uses_macos_application_support(self) -> None:
        with patch.object(source_paths.sys, "platform", "darwin"), patch("src.common.source_paths._is_macos", return_value=True):
            with patch.object(Path, "home", return_value=Path("/Users/tester")):
                self.assertEqual(
                    source_paths.claude_desktop_roots(),
                    [Path("/Users/tester/Library/Application Support/Claude/local-agent-mode-sessions")],
                )