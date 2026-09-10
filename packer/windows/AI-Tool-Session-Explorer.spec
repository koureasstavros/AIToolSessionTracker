from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPEC).parent.parent.parent

analysis = Analysis(
    [str(project_root / "session_token_viewer.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "src" / "common" / "model_costs.json"), "src/common"),
    ],
    hiddenimports=collect_submodules("src"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="AI-Tool-Session-Explorer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)