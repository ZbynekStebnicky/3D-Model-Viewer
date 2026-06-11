import os
from PyQt5.QtGui import QIcon, QPalette, QColor
from PyQt5.QtWidgets import (
    QMainWindow, QFileDialog, QAction, QActionGroup, QToolBar, QLabel,
    QMessageBox, QColorDialog, QApplication, QDockWidget, QWidget,
    QFormLayout, QTreeWidget, QTreeWidgetItem, QProgressBar,
    QMenu, QCheckBox, QRadioButton, QGroupBox, QVBoxLayout, QHBoxLayout,
    QSlider,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal

from occ_viewer import OCCViewer
from loaders import load_file_tree, FILE_FILTER


class _LoaderThread(QThread):
    shape_ready = pyqtSignal(object)
    load_error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        try:
            import math as _math
            from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
            from OCC.Core.Bnd import Bnd_Box
            from OCC.Core.BRepBndLib import brepbndlib
            self.progress.emit("Reading file…")
            root = load_file_tree(self._path)
            self.progress.emit("Meshing…")
            # Use relative deflection based on bbox diagonal so tiny and large
            # models both get sensible mesh quality (not a fixed 0.1 mm blob).
            try:
                box = Bnd_Box()
                brepbndlib.Add(root.shape, box)
                if not box.IsVoid():
                    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
                    diag = _math.sqrt(
                        (xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2
                    )
                    deflection = max(diag * 0.001, 1e-5)
                else:
                    deflection = 0.1
            except Exception:
                deflection = 0.1
            BRepMesh_IncrementalMesh(root.shape, deflection, False, 0.5, True)
            self.shape_ready.emit(root)
        except Exception as exc:
            self.load_error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("3D Model Viewer")
        self.resize(1280, 800)
        self._light_style = QApplication.instance().style().objectName()
        _here = os.path.dirname(os.path.abspath(__file__))
        _logo = os.path.join(_here, "logo.png")
        if os.path.isfile(_logo):
            self.setWindowIcon(QIcon(_logo))
        self.setAcceptDrops(True)
        self._loader = None
        self._is_busy = False
        self._current_path = None
        self._file_status = ""
        self._measure_qcolor = None
        self._unit_scale = 1.0
        self._unit_suffix = "mm"
        self._bbox_raw = None   # (dx, dy, dz) in model units, for unit refresh
        self._ais_to_item = {}  # maps ais_shape object → QTreeWidgetItem

        self._setup_viewer()
        self._setup_menus()
        self._setup_toolbar()
        self._setup_statusbar()
        self._setup_info_dock()
        self._setup_assembly_dock()
        self._setup_section_dock()

    # ── setup ─────────────────────────────────────────────────────────────────

    def _setup_viewer(self):
        self.viewer = OCCViewer(self)
        self.setCentralWidget(self.viewer)
        QTimer.singleShot(0, self.viewer.initialize)
        self.viewer.measurement_done.connect(self._on_measurement_done)
        self.viewer.angle_done.connect(self._on_angle_done)
        self.viewer.measure_point.connect(self._on_measure_point)
        self.viewer.measure_cancelled.connect(self._on_measure_cancelled)
        self.viewer.shape_picked.connect(self._on_shape_picked)
        self.viewer.edge_measured.connect(self._on_edge_measured)
        self.viewer.face_measured.connect(self._on_face_measured)

    def _setup_menus(self):
        bar = self.menuBar()

        # File
        file_menu = bar.addMenu("&File")
        self._open_act = QAction("&Open…", self)
        self._open_act.setShortcut("Ctrl+O")
        self._open_act.triggered.connect(self.open_file)
        file_menu.addAction(self._open_act)
        file_menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # View
        view_menu = bar.addMenu("&View")

        fit_act = QAction("&Fit All", self)
        fit_act.setShortcut("F")
        fit_act.triggered.connect(self.viewer.fit_all)
        view_menu.addAction(fit_act)
        view_menu.addSeparator()

        wire_act = QAction("&Wireframe", self)
        wire_act.setShortcut("W")
        wire_act.triggered.connect(self.viewer.set_wireframe)
        view_menu.addAction(wire_act)

        shaded_act = QAction("&Shaded", self)
        shaded_act.setShortcut("S")
        shaded_act.triggered.connect(self.viewer.set_shaded)
        view_menu.addAction(shaded_act)

        edges_act = QAction("Shaded with &Edges", self)
        edges_act.setShortcut("E")
        edges_act.triggered.connect(self.viewer.set_shaded_with_edges)
        view_menu.addAction(edges_act)
        view_menu.addSeparator()

        for label, shortcut, slot in [
            ("&Top",       "Num+7", self.viewer.view_top),
            ("&Front",     "Num+1", self.viewer.view_front),
            ("&Right",     "Num+3", self.viewer.view_right),
            ("&Isometric", "Num+0", self.viewer.view_iso),
        ]:
            act = QAction(label, self)
            act.setShortcut(shortcut)
            act.triggered.connect(slot)
            view_menu.addAction(act)

        view_menu.addSeparator()
        # Panel toggles and dark mode are inserted here by the dock setup methods
        self._view_menu = view_menu

        self._dark_act = QAction("&Dark Mode", self)
        self._dark_act.setCheckable(True)
        self._dark_act.setShortcut("Ctrl+D")
        self._dark_act.toggled.connect(self._apply_dark_mode)
        view_menu.addAction(self._dark_act)

        view_menu.addSeparator()
        bg_act = QAction("Background &Color…", self)
        bg_act.triggered.connect(self._pick_background_color)
        view_menu.addAction(bg_act)

        # Measure
        measure_menu = bar.addMenu("&Measure")

        self._measure_act = QAction("&Distance", self)
        self._measure_act.setShortcut("M")
        self._measure_act.setCheckable(True)
        self._measure_act.setToolTip("Click two points to measure distance (M)")
        self._measure_act.toggled.connect(self._on_measure_toggled)
        measure_menu.addAction(self._measure_act)

        self._angle_act = QAction("&Angle", self)
        self._angle_act.setShortcut("A")
        self._angle_act.setCheckable(True)
        self._angle_act.setToolTip("Click 3 points to measure angle: arm → vertex → arm (A)")
        self._angle_act.toggled.connect(self._on_angle_toggled)
        measure_menu.addAction(self._angle_act)

        self._edge_face_act = QAction("Edge/&Face", self)
        self._edge_face_act.setShortcut("G")
        self._edge_face_act.setCheckable(True)
        self._edge_face_act.setToolTip(
            "Click an edge for length (and radius if circular), or a face for area (G)"
        )
        self._edge_face_act.toggled.connect(self._on_edge_face_toggled)
        measure_menu.addAction(self._edge_face_act)

        measure_menu.addSeparator()

        units_menu = measure_menu.addMenu("&Units")
        units_group = QActionGroup(self)
        units_group.setExclusive(True)
        for label, scale, suffix in [
            ("&mm",  1.0,          "mm"),
            ("&cm",  0.1,          "cm"),
            ("&m",   0.001,        "m"),
            ("&in",  0.0393701,    "in"),
        ]:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setData((scale, suffix))
            units_group.addAction(a)
            units_menu.addAction(a)
        units_group.actions()[0].setChecked(True)
        units_group.triggered.connect(self._on_unit_changed)

        measure_menu.addSeparator()

        color_act = QAction("Measurement &Color…", self)
        color_act.triggered.connect(self._pick_measure_color)
        measure_menu.addAction(color_act)

        measure_menu.addSeparator()

        clear_act = QAction("&Clear All", self)
        clear_act.setShortcut("Ctrl+M")
        clear_act.triggered.connect(self._on_clear_measures)
        measure_menu.addAction(clear_act)

    def _setup_toolbar(self):
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        self._open_tb_act = QAction("Open", self)
        self._open_tb_act.setToolTip("Open CAD file (Ctrl+O)")
        self._open_tb_act.triggered.connect(self.open_file)
        tb.addAction(self._open_tb_act)

        tb.addSeparator()

        for label, tip, slot in [
            ("Fit All", "Fit all (F)", self.viewer.fit_all),
        ]:
            a = QAction(label, self)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            tb.addAction(a)

        tb.addSeparator()

        for label, tip, slot in [
            ("Wireframe",    "Wireframe mode (W)",        self.viewer.set_wireframe),
            ("Shaded",       "Shaded mode (S)",           self.viewer.set_shaded),
            ("Shaded+Edges", "Shaded with edges (E)",     self.viewer.set_shaded_with_edges),
        ]:
            a = QAction(label, self)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            tb.addAction(a)

        tb.addSeparator()

        for label, tip, slot in [
            ("Top",   "View from top (Num 7)",   self.viewer.view_top),
            ("Front", "View from front (Num 1)", self.viewer.view_front),
            ("Right", "View from right (Num 3)", self.viewer.view_right),
            ("Iso",   "Isometric (Num 0)",        self.viewer.view_iso),
        ]:
            a = QAction(label, self)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            tb.addAction(a)

        tb.addSeparator()

        self._measure_tb_act = QAction("Distance", self)
        self._measure_tb_act.setToolTip(
            "Measure distance between two points (M)\n"
            "Shift+drag an annotation to reposition it"
        )
        self._measure_tb_act.setCheckable(True)
        self._measure_tb_act.toggled.connect(self._measure_act.setChecked)
        self._measure_act.toggled.connect(self._measure_tb_act.setChecked)
        tb.addAction(self._measure_tb_act)

        self._angle_tb_act = QAction("Angle", self)
        self._angle_tb_act.setToolTip("Measure angle: arm → vertex → arm (A)")
        self._angle_tb_act.setCheckable(True)
        self._angle_tb_act.toggled.connect(self._angle_act.setChecked)
        self._angle_act.toggled.connect(self._angle_tb_act.setChecked)
        tb.addAction(self._angle_tb_act)

        self._edge_face_tb_act = QAction("Edge/Face", self)
        self._edge_face_tb_act.setToolTip(
            "Click an edge for length/radius, or a face for area (G)"
        )
        self._edge_face_tb_act.setCheckable(True)
        self._edge_face_tb_act.toggled.connect(self._edge_face_act.setChecked)
        self._edge_face_act.toggled.connect(self._edge_face_tb_act.setChecked)
        tb.addAction(self._edge_face_tb_act)

        clear_tb = QAction("Clear", self)
        clear_tb.setToolTip("Clear all measurements (Ctrl+M)")
        clear_tb.triggered.connect(self._on_clear_measures)
        tb.addAction(clear_tb)

    def _setup_statusbar(self):
        self._status = QLabel("No file loaded – open a STEP, IGES, or BRep file")
        self.statusBar().addWidget(self._status, 1)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)   # marquee – duration unknown
        self._progress_bar.setMaximumWidth(180)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.hide()
        self.statusBar().addPermanentWidget(self._progress_bar)

    def _setup_info_dock(self):
        self._info_dock = QDockWidget("Model Info", self)
        self._info_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._info_dock.setMinimumWidth(170)

        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(4)
        form.setLabelAlignment(Qt.AlignRight)

        self._info = {}
        for key in ["File", "Format", "Faces", "Edges", "Vertices",
                    "Size X", "Size Y", "Size Z"]:
            lbl = QLabel("–")
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setWordWrap(True)
            form.addRow(key + ":", lbl)
            self._info[key] = lbl

        self._info_dock.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, self._info_dock)

        toggle = self._info_dock.toggleViewAction()
        toggle.setText("&Model Info Panel")
        self._view_menu.insertAction(self._dark_act, toggle)
        self._view_menu.insertSeparator(self._dark_act)

    def _setup_assembly_dock(self):
        self._asm_dock = QDockWidget("Assembly", self)
        self._asm_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._asm_dock.setMinimumWidth(200)

        self._asm_tree = QTreeWidget()
        self._asm_tree.setHeaderHidden(True)
        self._asm_tree.itemChanged.connect(self._on_asm_item_changed)
        self._asm_tree.itemClicked.connect(self._on_tree_item_clicked)
        self._asm_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._asm_tree.customContextMenuRequested.connect(self._on_asm_context_menu)

        self._asm_dock.setWidget(self._asm_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self._asm_dock)

        toggle = self._asm_dock.toggleViewAction()
        toggle.setText("&Assembly Panel")
        self._view_menu.insertAction(self._dark_act, toggle)

    def _setup_section_dock(self):
        self._section_dock = QDockWidget("Section View", self)
        self._section_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self._section_dock.setMinimumWidth(200)

        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setContentsMargins(8, 8, 8, 8)
        vlay.setSpacing(6)

        self._section_enabled = QCheckBox("Enable")
        self._section_enabled.toggled.connect(self._on_section_toggled)
        vlay.addWidget(self._section_enabled)

        axis_box = QGroupBox("Axis")
        hlay = QHBoxLayout(axis_box)
        self._axis_btns = {}
        for ax in ('X', 'Y', 'Z'):
            rb = QRadioButton(ax)
            hlay.addWidget(rb)
            self._axis_btns[ax] = rb
        self._axis_btns['Z'].setChecked(True)
        for ax, rb in self._axis_btns.items():
            rb.toggled.connect(lambda checked, a=ax: self._on_section_axis_changed(a, checked))
        vlay.addWidget(axis_box)

        vlay.addWidget(QLabel("Position:"))
        self._section_slider = QSlider(Qt.Horizontal)
        self._section_slider.setRange(0, 1000)
        self._section_slider.setValue(500)
        self._section_slider.valueChanged.connect(self._on_section_slider)
        vlay.addWidget(self._section_slider)
        vlay.addStretch()

        self._section_dock.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, self._section_dock)
        self._section_dock.hide()  # hidden by default

        toggle = self._section_dock.toggleViewAction()
        toggle.setText("&Section View Panel")
        self._view_menu.insertAction(self._dark_act, toggle)

    def _populate_assembly_tree(self, root):
        self._asm_tree.blockSignals(True)
        self._asm_tree.clear()
        self._ais_to_item = {}

        def make_item(parent, node, inherited_ais):
            ais = node.ais_shape if node.ais_shape is not None else inherited_ais
            item = QTreeWidgetItem([node.name])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked)
            item.setData(0, Qt.UserRole, ais)
            parent.addChild(item)
            if ais is not None:
                # setdefault: parent wins over children sharing the same AIS,
                # so viewport picks highlight the outermost owning item.
                self._ais_to_item.setdefault(ais, item)
            for child in node.children:
                make_item(item, child, ais)

        make_item(self._asm_tree.invisibleRootItem(), root, None)
        self._asm_tree.expandAll()
        self._asm_tree.blockSignals(False)

    def _on_asm_item_changed(self, item, column):
        if column != 0:
            return
        visible = item.checkState(0) == Qt.Checked
        ais = item.data(0, Qt.UserRole)
        if ais is not None:
            self.viewer.set_part_visible(ais, visible)
        else:
            self._asm_tree.blockSignals(True)
            self._toggle_subtree(item, visible)
            self._asm_tree.blockSignals(False)

    def _toggle_subtree(self, item, visible: bool):
        check = Qt.Checked if visible else Qt.Unchecked
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, check)
            ais = child.data(0, Qt.UserRole)
            if ais is not None:
                self.viewer.set_part_visible(ais, visible)
            else:
                self._toggle_subtree(child, visible)

    def _on_tree_item_clicked(self, item, _column):
        ais = item.data(0, Qt.UserRole)
        if ais is None:
            return
        self.viewer.select_ais(ais)

    def _on_shape_picked(self, ais):
        if ais is None:
            self._asm_tree.clearSelection()
            return
        item = self._ais_to_item.get(ais)
        if item is None:
            return
        self._asm_tree.blockSignals(True)
        self._asm_tree.setCurrentItem(item)
        self._asm_tree.scrollToItem(item)
        self._asm_tree.blockSignals(False)

    def _on_asm_context_menu(self, pos):
        item = self._asm_tree.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            menu.addAction("Isolate").triggered.connect(
                lambda: self._isolate_item(item)
            )
            menu.addAction("Hide").triggered.connect(
                lambda: item.setCheckState(0, Qt.Unchecked)
            )
            menu.addSeparator()
        menu.addAction("Show All").triggered.connect(self._show_all_parts)
        menu.exec_(self._asm_tree.mapToGlobal(pos))

    def _isolate_item(self, item):
        target_ais = item.data(0, Qt.UserRole)
        if target_ais is None:
            return
        self._asm_tree.blockSignals(True)

        def visit(parent):
            for i in range(parent.childCount()):
                child = parent.child(i)
                ais = child.data(0, Qt.UserRole)
                if ais is not None:
                    visible = ais is target_ais
                    child.setCheckState(0, Qt.Checked if visible else Qt.Unchecked)
                    self.viewer.set_part_visible(ais, visible)
                visit(child)

        visit(self._asm_tree.invisibleRootItem())
        self._asm_tree.blockSignals(False)

    def _show_all_parts(self):
        self._asm_tree.blockSignals(True)

        def visit(parent):
            for i in range(parent.childCount()):
                child = parent.child(i)
                child.setCheckState(0, Qt.Checked)
                ais = child.data(0, Qt.UserRole)
                if ais is not None:
                    self.viewer.set_part_visible(ais, True)
                visit(child)

        visit(self._asm_tree.invisibleRootItem())
        self._asm_tree.blockSignals(False)

    # ── section view ──────────────────────────────────────────────────────────

    def _current_clip_axis(self) -> str:
        for ax, rb in self._axis_btns.items():
            if rb.isChecked():
                return ax
        return 'Z'

    def _on_section_toggled(self, enabled: bool):
        if enabled:
            t = self._section_slider.value() / 1000.0
            self.viewer.set_clip_plane(self._current_clip_axis(), t)
        else:
            self.viewer.remove_clip_plane()

    def _on_section_axis_changed(self, axis: str, checked: bool):
        if checked and self._section_enabled.isChecked():
            t = self._section_slider.value() / 1000.0
            self.viewer.set_clip_plane(axis, t)

    def _on_section_slider(self, value: int):
        if self._section_enabled.isChecked():
            self.viewer.set_clip_plane(self._current_clip_axis(), value / 1000.0)

    # ── file loading ──────────────────────────────────────────────────────────

    def open_file(self):
        start_dir = os.path.dirname(self._current_path) if self._current_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open CAD File", start_dir, FILE_FILTER
        )
        if path:
            self._load(path)

    def _load(self, path: str):
        self._current_path = path
        self._set_busy(True)
        self._status.setText(f"Loading {os.path.basename(path)} …")

        self._loader = _LoaderThread(path, self)
        self._loader.shape_ready.connect(lambda shape: self._on_ready(path, shape))
        self._loader.load_error.connect(self._on_error)
        self._loader.progress.connect(self._on_load_progress)
        self._loader.start()

    def _on_ready(self, path: str, root):
        if path != self._current_path:
            self._set_busy(False)
            return
        # Disable section view so the stale clip plane doesn't survive a reload
        self._section_enabled.blockSignals(True)
        self._section_enabled.setChecked(False)
        self._section_enabled.blockSignals(False)
        try:
            self._on_load_progress("Displaying…")
            QApplication.processEvents()
            self.viewer.display_assembly(root)
            self._populate_assembly_tree(root)
            self.setWindowTitle(f"3D Model Viewer – {os.path.basename(path)}")
            self._update_file_status(path, root.shape)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Display Error", str(exc))
        finally:
            self._set_busy(False)

    def _on_error(self, msg: str):
        QMessageBox.critical(self, "Load Error", msg)
        self._status.setText("Load failed")
        self._set_busy(False)

    def _on_load_progress(self, stage: str):
        self._status.setText(f"{os.path.basename(self._current_path)} – {stage}")

    def _set_busy(self, busy: bool):
        self._is_busy = busy
        self._open_act.setEnabled(not busy)
        self._open_tb_act.setEnabled(not busy)
        if busy:
            self._progress_bar.show()
        else:
            self._progress_bar.hide()

    def _update_file_status(self, path: str, shape):
        from OCC.Core.TopExp import TopExp_Explorer
        from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX

        def count(topo_type):
            exp = TopExp_Explorer(shape, topo_type)
            n = 0
            while exp.More():
                n += 1
                exp.Next()
            return n

        faces    = count(TopAbs_FACE)
        edges    = count(TopAbs_EDGE)
        vertices = count(TopAbs_VERTEX)
        self._file_status = (
            f"{os.path.basename(path)}   |   "
            f"Faces: {faces}   Edges: {edges}   Vertices: {vertices}"
        )
        self._status.setText(self._file_status)
        self._update_model_info_dock(path, shape, faces, edges, vertices)

    def _update_model_info_dock(self, path: str, shape, faces: int, edges: int, vertices: int):
        from OCC.Core.Bnd import Bnd_Box
        from OCC.Core.BRepBndLib import brepbndlib

        ext = os.path.splitext(path)[1].lower()
        fmt = {".stp": "STEP", ".step": "STEP",
               ".igs": "IGES", ".iges": "IGES",
               ".brep": "BRep"}.get(ext, ext.upper().lstrip("."))

        try:
            box = Bnd_Box()
            brepbndlib.Add(shape, box)
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            self._bbox_raw = (xmax - xmin, ymax - ymin, zmax - zmin)
        except Exception:
            self._bbox_raw = None

        self._info["File"].setText(os.path.basename(path))
        self._info["Format"].setText(fmt)
        self._info["Faces"].setText(f"{faces:,}")
        self._info["Edges"].setText(f"{edges:,}")
        self._info["Vertices"].setText(f"{vertices:,}")
        self._refresh_size_labels()

    def _refresh_size_labels(self):
        if self._bbox_raw is None:
            self._info["Size X"].setText("–")
            self._info["Size Y"].setText("–")
            self._info["Size Z"].setText("–")
            return
        dx, dy, dz = self._bbox_raw
        s, u = self._unit_scale, self._unit_suffix
        self._info["Size X"].setText(f"{dx * s:.2f} {u}")
        self._info["Size Y"].setText(f"{dy * s:.2f} {u}")
        self._info["Size Z"].setText(f"{dz * s:.2f} {u}")

    # ── measurement ───────────────────────────────────────────────────────────

    def _on_measure_toggled(self, checked: bool):
        self._measure_tb_act.blockSignals(True)
        self._measure_tb_act.setChecked(checked)
        self._measure_tb_act.blockSignals(False)
        if checked:
            for act in (self._angle_act, self._angle_tb_act,
                        self._edge_face_act, self._edge_face_tb_act):
                act.blockSignals(True)
                act.setChecked(False)
                act.blockSignals(False)
            self.viewer.start_measure_distance()
            self._status.setText(
                "Distance: click first point   |   Right-click to restart   |   M to exit"
            )
        else:
            if not self._angle_act.isChecked() and not self._edge_face_act.isChecked():
                self.viewer.stop_measure()
            self._status.setText(self._file_status or "No file loaded")

    def _on_angle_toggled(self, checked: bool):
        self._angle_tb_act.blockSignals(True)
        self._angle_tb_act.setChecked(checked)
        self._angle_tb_act.blockSignals(False)
        if checked:
            for act in (self._measure_act, self._measure_tb_act,
                        self._edge_face_act, self._edge_face_tb_act):
                act.blockSignals(True)
                act.setChecked(False)
                act.blockSignals(False)
            self.viewer.start_measure_angle()
            self._status.setText(
                "Angle: click first arm point (1/3)   |   Right-click to restart   |   A to exit"
            )
        else:
            if not self._measure_act.isChecked() and not self._edge_face_act.isChecked():
                self.viewer.stop_measure()
            self._status.setText(self._file_status or "No file loaded")

    def _on_edge_face_toggled(self, checked: bool):
        self._edge_face_tb_act.blockSignals(True)
        self._edge_face_tb_act.setChecked(checked)
        self._edge_face_tb_act.blockSignals(False)
        if checked:
            for act in (self._measure_act, self._measure_tb_act,
                        self._angle_act, self._angle_tb_act):
                act.blockSignals(True)
                act.setChecked(False)
                act.blockSignals(False)
            self.viewer.start_measure_edge_face()
            self._status.setText(
                "Edge/Face: click an edge (length) or face (area)   |   G to exit"
            )
        else:
            if not self._measure_act.isChecked() and not self._angle_act.isChecked():
                self.viewer.stop_measure()
            self._status.setText(self._file_status or "No file loaded")

    def _on_measure_point(self, n: int):
        if self._measure_act.isChecked():
            if n == 1:
                self._status.setText(
                    "Distance: click second point   |   Right-click to restart   |   M to exit"
                )
        elif self._angle_act.isChecked():
            if n == 1:
                self._status.setText(
                    "Angle: click vertex point (2/3)   |   Right-click to restart   |   A to exit"
                )
            elif n == 2:
                self._status.setText(
                    "Angle: click second arm point (3/3)   |   Right-click to restart   |   A to exit"
                )

    def _on_unit_changed(self, action: QAction):
        self._unit_scale, self._unit_suffix = action.data()
        self._refresh_size_labels()

    def _on_measurement_done(self, distance: float):
        val = distance * self._unit_scale
        self._status.setText(
            f"Distance: {val:.3f} {self._unit_suffix}"
            f"   |   Click next first point   |   Right-click to restart   |   M to exit"
        )

    def _on_angle_done(self, angle_deg: float):
        self._status.setText(
            f"Angle: {angle_deg:.2f}°   |   Click next first arm point   |   Right-click to restart   |   A to exit"
        )

    def _on_edge_measured(self, length: float, radius: float):
        val = length * self._unit_scale
        u = self._unit_suffix
        if radius > 0:
            r_val = radius * self._unit_scale
            self._status.setText(
                f"Edge: {val:.3f} {u}   |   Radius: {r_val:.3f} {u}   |   G to exit"
            )
        else:
            self._status.setText(f"Edge length: {val:.3f} {u}   |   G to exit")

    def _on_face_measured(self, area: float):
        val = area * (self._unit_scale ** 2)
        self._status.setText(
            f"Face area: {val:.3f} {self._unit_suffix}²   |   G to exit"
        )

    def _on_measure_cancelled(self):
        if self._measure_act.isChecked():
            self._status.setText(
                "Distance: click first point   |   Right-click to restart   |   M to exit"
            )
        elif self._angle_act.isChecked():
            self._status.setText(
                "Angle: click first arm point (1/3)   |   Right-click to restart   |   A to exit"
            )

    def _on_clear_measures(self):
        self.viewer.clear_measurements()
        if self._measure_act.isChecked():
            self._status.setText(
                "Distance: click first point   |   Right-click to restart   |   M to exit"
            )
        elif self._angle_act.isChecked():
            self._status.setText(
                "Angle: click first arm point (1/3)   |   Right-click to restart   |   A to exit"
            )
        elif self._edge_face_act.isChecked():
            self._status.setText(
                "Edge/Face: click an edge (length) or face (area)   |   G to exit"
            )
        else:
            self._status.setText(self._file_status or "No file loaded")

    def _pick_measure_color(self):
        initial = self._measure_qcolor or QColor(255, 0, 0)
        color = QColorDialog.getColor(initial, parent=self, title="Measurement Color")
        if color.isValid():
            self._measure_qcolor = color
            self.viewer.set_measure_color(color.red(), color.green(), color.blue())

    # ── dark mode ─────────────────────────────────────────────────────────────

    def _apply_dark_mode(self, enabled: bool) -> None:
        app = QApplication.instance()
        if enabled:
            app.setStyle("Fusion")
            p = QPalette()
            p.setColor(QPalette.Window,          QColor(45,  45,  45))
            p.setColor(QPalette.WindowText,       QColor(220, 220, 220))
            p.setColor(QPalette.Base,             QColor(30,  30,  30))
            p.setColor(QPalette.AlternateBase,    QColor(45,  45,  45))
            p.setColor(QPalette.ToolTipBase,      QColor(45,  45,  45))
            p.setColor(QPalette.ToolTipText,      QColor(220, 220, 220))
            p.setColor(QPalette.Text,             QColor(220, 220, 220))
            p.setColor(QPalette.Button,           QColor(55,  55,  55))
            p.setColor(QPalette.ButtonText,       QColor(220, 220, 220))
            p.setColor(QPalette.BrightText,       QColor(255,  80,  80))
            p.setColor(QPalette.Link,             QColor(80,  160, 255))
            p.setColor(QPalette.Highlight,        QColor(0,   120, 215))
            p.setColor(QPalette.HighlightedText,  QColor(255, 255, 255))
            p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(110, 110, 110))
            p.setColor(QPalette.Disabled, QPalette.Text,       QColor(110, 110, 110))
            p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(110, 110, 110))
            app.setPalette(p)
        else:
            app.setStyle(self._light_style)
            app.setPalette(app.style().standardPalette())
        self._set_dark_titlebar(enabled)

    def _set_dark_titlebar(self, dark: bool) -> None:
        try:
            import ctypes
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            hwnd = int(self.winId())
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(value), ctypes.sizeof(value),
            )
        except Exception:
            pass

    # ── background colour ─────────────────────────────────────────────────────

    def _pick_background_color(self):
        color = QColorDialog.getColor(parent=self, title="Background Color")
        if color.isValid():
            self.viewer.set_background_color(color.red(), color.green(), color.blue())

    # ── drag and drop ─────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self._is_busy:
            return
        urls = event.mimeData().urls()
        if urls:
            self._load(urls[0].toLocalFile())

    def closeEvent(self, event):
        if self._loader is not None and self._loader.isRunning():
            self.hide()   # disappear immediately; don't freeze with window visible
            self._loader.wait()
        super().closeEvent(event)
