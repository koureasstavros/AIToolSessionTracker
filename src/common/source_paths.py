"""Platform-specific application storage locations used by providers."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    return Path(configured) if configured else Path.home() / ".config"


def extension_roots() -> list[Path]:
    """Return Extension storage directories for stable and Insiders installs."""
    if _is_windows():
        configured = os.environ.get("APPDATA")
        base = Path(configured) if configured else Path.home() / "AppData" / "Roaming"
    elif _is_macos():
        base = Path.home() / "Library" / "Application Support"
    else:
        base = _config_home()
    return [base / name / "User" for name in ("Code", "Code - Insiders")]


def claude_desktop_roots() -> list[Path]:
    """Return platform-specific Claude Desktop local-agent storage roots."""
    if _is_windows():
        configured = os.environ.get("LOCALAPPDATA")
        roots = [Path(configured)] if configured else []
        roots.append(Path.home() / "AppData" / "Local")
        return list(dict.fromkeys(root / "Claude-3p" / "local-agent-mode-sessions" for root in roots))
    if _is_macos():
        return [Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"]
    return [_config_home() / "Claude" / "local-agent-mode-sessions"]