# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for 3D Model Viewer
#
# Prerequisites (run once):
#   conda create -n viewer-build python=3.11
#   conda activate viewer-build
#   conda install -c conda-forge pythonocc-core
#   pip install PyQt5 pyinstaller
#
# Build:
#   conda activate viewer-build
#   cd "C:\Users\zbynek.stebnicky\Documents\Projekty\ModelViewer"
#   rmdir /s /q build dist
#   pyinstaller viewer.spec
#
# Output: dist/3D Model Viewer/   <- zip this folder to distribute

import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all

# ── Python-level OCC and PyQt5 collection ────────────────────────────────────
occ_datas, occ_binaries, occ_hiddenimports = collect_all('OCC')
qt_datas,  qt_binaries,  _                = collect_all('PyQt5')

# ── Native OpenCASCADE DLLs from conda ───────────────────────────────────────
# CONDA_PREFIX is set by "conda activate" and points directly at the active env.
# Deriving the path from sys.executable is fragile (depth differs per OS);
# the env-var is the canonical answer.
conda_prefix = os.environ.get('CONDA_PREFIX', '')
if not conda_prefix:
    # Fallback: on Windows, python.exe sits in the env root directly.
    conda_prefix = os.path.dirname(sys.executable)

lib_bin = os.path.join(conda_prefix, 'Library', 'bin')

print(f'[spec] CONDA_PREFIX  = {conda_prefix}')
print(f'[spec] Library\\bin   = {lib_bin}')
print(f'[spec] lib_bin exists = {os.path.isdir(lib_bin)}')

# Collect every DLL from Library\bin except MSVC / Windows API shims that
# must come from the OS anyway.  This captures all TK*.dll, freetype, tbb,
# libjpeg, zlib, etc. that OCC's .pyd files link against at load time.
_SYSTEM_PREFIXES = (
    'api-ms-win', 'ext-ms-win',
    'ucrtbase', 'msvcp', 'vcruntime', 'concrt',
    'd3d', 'dwrite', 'dwmapi', 'dxgi',
)
native_dlls = []
if os.path.isdir(lib_bin):
    for dll in glob.glob(os.path.join(lib_bin, '*.dll')):
        name = os.path.basename(dll).lower()
        if not any(name.startswith(p) for p in _SYSTEM_PREFIXES):
            native_dlls.append((dll, '.'))

print(f'[spec] native DLLs collected: {len(native_dlls)}')
if not native_dlls:
    print('[spec] WARNING: no native DLLs found — _AIS will fail at runtime!')
    print('[spec] Make sure you ran "conda activate viewer-build" before pyinstaller.')

# ── Analysis ─────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=occ_binaries + qt_binaries + native_dlls,
    datas=(
        occ_datas
        + qt_datas
        + [('logo.png', '.')]
    ),
    hiddenimports=(
        occ_hiddenimports
        + [
            'OCC.Display.backend',
            'OCC.Display.qtDisplay',
            'OCC.Display.pyqt5Renderer',
            'PyQt5.QtCore',
            'PyQt5.QtGui',
            'PyQt5.QtWidgets',
            'PyQt5.QtOpenGL',
            'PyQt5.sip',
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthook_occ.py'],
    excludes=[
        'tkinter', '_tkinter',
        'matplotlib', 'scipy', 'pandas',
        'IPython', 'notebook', 'jupyter_client',
        'wx', 'gtk',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='3D Model Viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='logo.ico',
    contents_directory='.',   # PyInstaller 6: flatten layout so TK DLLs land
                              # next to the exe (the Windows "application dir")
                              # instead of in _internal/
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='3D Model Viewer',
    contents_directory='.',   # PyInstaller 6: put everything next to the exe,
                              # not in _internal/, so TK DLLs land in the
                              # Windows "application directory" and the
                              # implicit TK→TK chain-load search finds them.
)
