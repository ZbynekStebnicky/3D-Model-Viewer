import os
import sys

if hasattr(sys, '_MEIPASS'):
    meipass = sys._MEIPASS

    # Register every directory inside the bundle that contains DLLs.
    # Walking subdirectories covers OCC\Core\, OCC\Display\, PyQt5\, etc.
    # Belt-and-suspenders: also prepend to PATH for any legacy LoadLibrary calls.
    if hasattr(os, 'add_dll_directory'):
        for _root, _, _files in os.walk(meipass):
            if any(f.lower().endswith('.dll') for f in _files):
                try:
                    os.add_dll_directory(_root)
                except Exception:
                    pass

    os.environ['PATH'] = meipass + os.pathsep + os.environ.get('PATH', '')
