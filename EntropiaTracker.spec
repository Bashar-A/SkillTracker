# Build a standalone Windows executable with the static reference catalogs.
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'entropia_tracker_ui.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'skills.json'), '.'), (str(root / 'professions.json'), '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='EntropiaTracker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
