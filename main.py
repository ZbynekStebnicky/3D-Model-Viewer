import os
import sys


def main():
    from OCC.Display.backend import load_backend
    load_backend("pyqt5")

    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QIcon
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("3D Model Viewer")
    _logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if os.path.isfile(_logo):
        app.setWindowIcon(QIcon(_logo))

    from main_window import MainWindow
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
