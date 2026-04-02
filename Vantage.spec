# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('parsers',      'parsers'),
    ('fonts',        'fonts'),
    ('settings.ini', '.'),
    ('icon.ico',     '.'),
    ('version.py',   '.'),
]
binaries = []
hiddenimports = [
    'email.mime.text',
    'email.mime.multipart',
    'email.mime.base',
    'imaplib',
    'matplotlib.backends.backend_agg',
    'matplotlib.backends.backend_qt5agg',
    'pkg_resources.py2_warn',
]

for pkg in ('PyQt6', 'matplotlib'):
    tmp = collect_all(pkg)
    datas     += tmp[0]
    binaries  += tmp[1]
    hiddenimports += tmp[2]

a = Analysis(
    ['gui_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Vantage',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Vantage',
)
