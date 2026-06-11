import math
from enum import Enum, auto

from PyQt5.QtCore import pyqtSignal, Qt
from OCC.Display.qtDisplay import qtViewer3d
from OCC.Core.TopoDS import TopoDS_Shape
from OCC.Core.gp import gp_Pnt, gp_Dir, gp_Vec, gp_Pln
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCC.Core.AIS import AIS_Point
from OCC.Core.PrsDim import PrsDim_LengthDimension, PrsDim_AngleDimension
from OCC.Core.Geom import Geom_CartesianPoint
from OCC.Core.Prs3d import Prs3d_PointAspect, Prs3d_DimensionAspect
from OCC.Core.Aspect import Aspect_TOM_BALL
from OCC.Core.Bnd import Bnd_Box
from OCC.Core.BRepBndLib import brepbndlib
from OCC.Core.BRep import BRep_Tool
from OCC.Core.TopoDS import topods
from OCC.Core.TopAbs import TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE
from OCC.Core.Graphic3d import Graphic3d_ZLayerId_Topmost


class _MeasureMode(Enum):
    NONE = auto()
    DISTANCE = auto()
    ANGLE = auto()
    EDGE_FACE = auto()


class _MeasureState(Enum):
    IDLE = auto()
    AWAITING_FIRST = auto()
    AWAITING_SECOND = auto()
    AWAITING_THIRD = auto()


class OCCViewer(qtViewer3d):
    measurement_done = pyqtSignal(float)    # distance in model units
    angle_done = pyqtSignal(float)           # angle in degrees
    measure_point = pyqtSignal(int)          # intermediate point collected (1, 2, …)
    measure_cancelled = pyqtSignal()
    shape_picked = pyqtSignal(object)        # AIS_Shape after normal left-click (None = deselect)
    edge_measured = pyqtSignal(float, float) # (length, radius_or_0) in model units
    face_measured = pyqtSignal(float)        # area in model units²

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_ais = None
        self._measure_mode = _MeasureMode.NONE
        self._measure_state = _MeasureState.IDLE
        self._measure_pts = []
        self._measure_ais = []
        self._measure_color = Quantity_Color(1.0, 0.0, 0.0, Quantity_TOC_RGB)
        # annotation drag state
        self._dragged_dim = None
        self._drag_start_y = 0
        self._drag_start_flyout = 0.0
        self._drag_scale = 1.0          # model-units per pixel (estimated from bbox)
        self._model_diag = 0.0          # bounding box diagonal in model units
        self._dim_objects = []           # only PrsDim objects (not point markers)
        self._all_ais = []              # all non-measure AIS shapes currently displayed
        self._root_shape = None         # root shape for bounding-box / drag-scale
        self._clip_plane = None         # active Graphic3d_ClipPlane or None

    # ── initialization ────────────────────────────────────────────────────────

    def initialize(self):
        self.InitDriver()
        try:
            self._display.set_bg_gradient_color([30, 30, 40], [70, 70, 90])
        except Exception:
            pass
        try:
            self._display.display_trihedron()
        except Exception:
            pass
        try:
            self._display.EnableAntiAliasing()
        except Exception:
            pass

    # ── shape display ─────────────────────────────────────────────────────────

    def display_shape(self, shape: TopoDS_Shape) -> None:
        self._remove_clip_plane_silent()
        self._display.EraseAll()
        self._measure_ais.clear()
        self._dim_objects.clear()
        self._measure_mode = _MeasureMode.NONE
        self._measure_state = _MeasureState.IDLE
        self._measure_pts = []
        self._all_ais.clear()
        ais_list = self._display.DisplayShape(shape, update=False)
        self._current_ais = ais_list[0] if ais_list else None
        if self._current_ais:
            self._all_ais.append(self._current_ais)
        self._root_shape = shape
        self._draw_edges()
        self._display.FitAll()
        self._update_drag_scale()

    def display_assembly(self, root) -> None:
        """Display one AIS_Shape per direct child (or the root if no children)."""
        self._remove_clip_plane_silent()
        self._display.EraseAll()
        self._measure_ais.clear()
        self._dim_objects.clear()
        self._measure_mode = _MeasureMode.NONE
        self._measure_state = _MeasureState.IDLE
        self._measure_pts = []
        self._all_ais.clear()
        self._current_ais = None

        top_nodes = root.children if root.children else [root]
        for node in top_nodes:
            if node.shape.IsNull():
                continue
            ais_list = self._display.DisplayShape(node.shape, update=False)
            if ais_list:
                node.ais_shape = ais_list[0]
                self._all_ais.append(ais_list[0])

        self._current_ais = self._all_ais[0] if self._all_ais else None
        self._root_shape = root.shape
        self._draw_edges()
        self._display.FitAll()
        self._update_drag_scale()

    def set_part_visible(self, ais_shape, visible: bool) -> None:
        if visible:
            self._display.Context.Display(ais_shape, True)
        else:
            self._display.Context.Erase(ais_shape, True)

    def _draw_edges(self) -> None:
        for ais in self._all_ais:
            try:
                ais.Attributes().SetFaceBoundaryDraw(True)
                self._display.Context.Redisplay(ais, False)
            except Exception:
                pass
        try:
            self._display.Context.UpdateCurrentViewer()
        except Exception:
            pass

    def _update_drag_scale(self) -> None:
        if self._root_shape is None:
            return
        try:
            box = Bnd_Box()
            brepbndlib.Add(self._root_shape, box)
            if box.IsVoid():
                return
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            diag_3d = math.sqrt((xmax-xmin)**2 + (ymax-ymin)**2 + (zmax-zmin)**2)
            diag_px = math.sqrt(self.width()**2 + self.height()**2)
            if diag_3d > 0 and diag_px > 0:
                self._drag_scale = diag_3d / diag_px
                self._model_diag = diag_3d
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_drag_scale()

    # ── view modes ────────────────────────────────────────────────────────────

    def set_measure_color(self, r: int, g: int, b: int) -> None:
        self._measure_color = Quantity_Color(r / 255.0, g / 255.0, b / 255.0, Quantity_TOC_RGB)

    def set_background_color(self, r: int, g: int, b: int) -> None:
        self._display.set_bg_gradient_color([r, g, b], [r, g, b])
        self._display.Repaint()

    def set_wireframe(self) -> None:
        self._display.SetModeWireFrame()

    def set_shaded(self) -> None:
        for ais in self._all_ais:
            try:
                ais.Attributes().SetFaceBoundaryDraw(False)
            except Exception:
                pass
        self._display.SetModeShaded()

    def set_shaded_with_edges(self) -> None:
        self._display.SetModeShaded()
        self._draw_edges()

    def fit_all(self) -> None:
        self._display.FitAll()

    def view_top(self) -> None:
        self._display.View_Top()
        self._display.FitAll()

    def view_front(self) -> None:
        self._display.View_Front()
        self._display.FitAll()

    def view_right(self) -> None:
        self._display.View_Right()
        self._display.FitAll()

    def view_iso(self) -> None:
        self._display.View_Iso()
        self._display.FitAll()

    # ── clipping plane ────────────────────────────────────────────────────────

    def set_clip_plane(self, axis: str, t: float) -> None:
        """Apply or update an axis-aligned clip plane. t in [0..1] across the model bbox."""
        if self._root_shape is None:
            return
        try:
            box = Bnd_Box()
            brepbndlib.Add(self._root_shape, box)
            if box.IsVoid():
                return
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            if axis == 'X':
                pos = xmin + t * (xmax - xmin)
                pln = gp_Pln(gp_Pnt(pos, 0, 0), gp_Dir(1, 0, 0))
            elif axis == 'Y':
                pos = ymin + t * (ymax - ymin)
                pln = gp_Pln(gp_Pnt(0, pos, 0), gp_Dir(0, 1, 0))
            else:
                pos = zmin + t * (zmax - zmin)
                pln = gp_Pln(gp_Pnt(0, 0, pos), gp_Dir(0, 0, 1))

            if self._clip_plane is None:
                from OCC.Core.Graphic3d import Graphic3d_ClipPlane
                self._clip_plane = Graphic3d_ClipPlane(pln)
                self._clip_plane.SetOn(True)
                self._display.View.AddClipPlane(self._clip_plane)
            else:
                try:
                    self._clip_plane.SetEquation(pln)
                except Exception:
                    # OCC version without gp_Pln overload – recreate
                    self._display.View.RemoveClipPlane(self._clip_plane)
                    from OCC.Core.Graphic3d import Graphic3d_ClipPlane
                    self._clip_plane = Graphic3d_ClipPlane(pln)
                    self._clip_plane.SetOn(True)
                    self._display.View.AddClipPlane(self._clip_plane)
            self._display.View.Redraw()
        except Exception as e:
            print(f"[clip] error: {e}")

    def remove_clip_plane(self) -> None:
        self._remove_clip_plane_silent()
        try:
            self._display.View.Redraw()
        except Exception:
            pass

    def _remove_clip_plane_silent(self) -> None:
        if self._clip_plane is None:
            return
        try:
            self._display.View.RemoveClipPlane(self._clip_plane)
        except Exception:
            pass
        self._clip_plane = None

    # ── measurement ───────────────────────────────────────────────────────────

    @property
    def is_measuring(self) -> bool:
        return self._measure_state != _MeasureState.IDLE

    def start_measure_distance(self) -> None:
        self._measure_mode = _MeasureMode.DISTANCE
        self._measure_state = _MeasureState.AWAITING_FIRST
        self._measure_pts = []
        self._set_sub_selection(True)

    def start_measure_angle(self) -> None:
        self._measure_mode = _MeasureMode.ANGLE
        self._measure_state = _MeasureState.AWAITING_FIRST
        self._measure_pts = []
        self._set_sub_selection(True)

    def start_measure_edge_face(self) -> None:
        self._measure_mode = _MeasureMode.EDGE_FACE
        self._measure_state = _MeasureState.AWAITING_FIRST
        self._measure_pts = []
        ctx = self._display.Context
        for ais in self._all_ais:
            try:
                ctx.Deactivate(ais)
                ctx.Activate(ais, 2)  # edge
                ctx.Activate(ais, 4)  # face
            except Exception as e:
                print(f"[measure] selection mode error: {e}")

    def stop_measure(self) -> None:
        self._measure_mode = _MeasureMode.NONE
        self._measure_state = _MeasureState.IDLE
        self._measure_pts = []
        self._set_sub_selection(False)

    def _set_sub_selection(self, enable: bool) -> None:
        ctx = self._display.Context
        for ais in self._all_ais:
            try:
                ctx.Deactivate(ais)
                if enable:
                    ctx.Activate(ais, 1)
                    ctx.Activate(ais, 2)
                    ctx.Activate(ais, 4)
                else:
                    ctx.Activate(ais, 0)
            except Exception as e:
                print(f"[measure] selection mode error: {e}")

    def clear_measurements(self) -> None:
        ctx = self._display.Context
        for ais in self._measure_ais:
            try:
                ctx.Remove(ais, False)
            except Exception:
                pass
        try:
            ctx.UpdateCurrentViewer()
        except Exception:
            pass
        self._measure_ais.clear()
        self._dim_objects.clear()
        self._measure_pts = []

    # ── mouse events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # Shift+left: start flyout drag on nearest annotation
        if (self._measure_state == _MeasureState.IDLE
                and event.button() == Qt.LeftButton
                and event.modifiers() & Qt.ShiftModifier):
            dim = self._find_dim_near(event.pos().x(), event.pos().y())
            if dim is not None:
                self._dragged_dim = dim
                self._drag_start_y = event.pos().y()
                try:
                    self._drag_start_flyout = dim.GetFlyout()
                except Exception:
                    self._drag_start_flyout = 0.0

    def mouseMoveEvent(self, event):
        if self._dragged_dim is not None:
            dy = self._drag_start_y - event.pos().y()
            new_flyout = self._drag_start_flyout + dy * self._drag_scale
            try:
                self._dragged_dim.SetFlyout(new_flyout)
                self._display.Context.Redisplay(self._dragged_dim, True)
            except Exception:
                pass
            return  # suppress orbit while dragging annotation
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragged_dim is not None:
            self._dragged_dim = None
            return
        if self._measure_state != _MeasureState.IDLE:
            if event.button() == Qt.RightButton:
                self._measure_state = _MeasureState.AWAITING_FIRST
                self._measure_pts = []
                self.measure_cancelled.emit()
                return
            if event.button() == Qt.LeftButton:
                pt = event.pos()
                dx = abs(pt.x() - getattr(self, 'dragStartPosX', pt.x()))
                dy = abs(pt.y() - getattr(self, 'dragStartPosY', pt.y()))
                if dx <= 5 and dy <= 5:
                    if self._measure_mode == _MeasureMode.EDGE_FACE:
                        shape = self._pick_shape(pt.x(), pt.y())
                        if shape is not None:
                            self._handle_edge_face_click(shape)
                    else:
                        pick_pt = self._pick_point(pt.x(), pt.y())
                        if pick_pt is not None:
                            self._handle_measure_click(pick_pt)
                return
        super().mouseReleaseEvent(event)
        # Emit which AIS was selected so the tree can sync
        if event.button() == Qt.LeftButton:
            x, y = event.pos().x(), event.pos().y()
            dx = abs(x - getattr(self, 'dragStartPosX', x))
            dy = abs(y - getattr(self, 'dragStartPosY', y))
            if dx <= 5 and dy <= 5:
                self._emit_viewport_selection()

    def _emit_viewport_selection(self) -> None:
        ctx = self._display.Context
        try:
            ctx.InitSelected()
            if not ctx.MoreSelected():
                self.shape_picked.emit(None)
                return
            sel_ais = ctx.SelectedInteractive()
            # ctx.SelectedInteractive() returns a fresh wrapper, so identity
            # comparison fails; match by shape equality to get our cached object.
            for ais in self._all_ais:
                try:
                    if ais.Shape().IsSame(sel_ais.Shape()):
                        self.shape_picked.emit(ais)
                        return
                except Exception:
                    pass
        except Exception:
            pass

    def select_ais(self, ais) -> None:
        """Programmatically highlight an AIS shape in the viewport."""
        ctx = self._display.Context
        try:
            ctx.ClearSelected(False)
            ctx.SetSelected(ais, True)
        except Exception:
            try:
                ctx.AddOrRemoveSelected(ais, True)
            except Exception:
                pass

    def _pick_point(self, x: int, y: int):
        ctx = self._display.Context
        ctx.MoveTo(x, y, self._display.View, True)
        if not ctx.HasDetectedShape():
            return None

        shape = ctx.DetectedShape()

        # Vertices: snap to exact geometry
        if shape.ShapeType() == TopAbs_VERTEX:
            try:
                return BRep_Tool.Pnt(topods.Vertex(shape))
            except Exception:
                pass

        # Edges/faces: use the actual surface intersection point from the selector
        try:
            pt = ctx.MainSelector().PickedPoint(1)
            if pt is not None:
                return pt
        except Exception:
            pass

        # Fallback: bbox center
        box = Bnd_Box()
        brepbndlib.Add(shape, box)
        if box.IsVoid():
            return None
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        return gp_Pnt(
            (xmin + xmax) / 2,
            (ymin + ymax) / 2,
            (zmin + zmax) / 2,
        )

    def _pick_shape(self, x: int, y: int):
        ctx = self._display.Context
        ctx.MoveTo(x, y, self._display.View, True)
        if not ctx.HasDetectedShape():
            return None
        return ctx.DetectedShape()

    def _project_to_screen(self, pt: gp_Pnt):
        try:
            sx, sy = self._display.View.Convert(pt.X(), pt.Y(), pt.Z())
            return sx, sy
        except Exception:
            return None

    def _find_dim_near(self, x: int, y: int, threshold: int = 40) -> object:
        best = None
        best_dist = threshold
        for dim in self._dim_objects:
            try:
                ref = dim.GetTextPosition()
            except Exception:
                continue
            result = self._project_to_screen(ref)
            if result is None:
                continue
            sx, sy = result
            dist = math.sqrt((sx - x) ** 2 + (sy - y) ** 2)
            if dist < best_dist:
                best_dist = dist
                best = dim
        return best

    # ── measurement helpers ───────────────────────────────────────────────────

    def _handle_measure_click(self, pt: gp_Pnt) -> None:
        self._add_point_marker(pt)
        self._measure_pts.append(pt)

        if self._measure_state == _MeasureState.AWAITING_FIRST:
            self._measure_state = _MeasureState.AWAITING_SECOND
            self.measure_point.emit(1)

        elif self._measure_state == _MeasureState.AWAITING_SECOND:
            if self._measure_mode == _MeasureMode.DISTANCE:
                self._finish_distance(self._measure_pts[0], self._measure_pts[1])
                self._measure_pts = []
                self._measure_state = _MeasureState.AWAITING_FIRST
            else:
                self._measure_state = _MeasureState.AWAITING_THIRD
                self.measure_point.emit(2)

        elif self._measure_state == _MeasureState.AWAITING_THIRD:
            self._finish_angle(self._measure_pts[0], self._measure_pts[1], self._measure_pts[2])
            self._measure_pts = []
            self._measure_state = _MeasureState.AWAITING_FIRST

    def _handle_edge_face_click(self, shape) -> None:
        from OCC.Core.GProp import GProp_GProps
        from OCC.Core.BRepGProp import brepgprop

        stype = shape.ShapeType()
        if stype == TopAbs_EDGE:
            try:
                props = GProp_GProps()
                brepgprop.LinearProperties(shape, props)
                length = props.Mass()
            except Exception as e:
                print(f"[measure] edge length error: {e}")
                return

            radius = 0.0
            try:
                from OCC.Core.BRepAdaptor import BRepAdaptor_Curve
                from OCC.Core.GeomAbs import GeomAbs_Circle
                adaptor = BRepAdaptor_Curve(topods.Edge(shape))
                if adaptor.GetType() == GeomAbs_Circle:
                    radius = adaptor.Circle().Radius()
            except Exception:
                pass

            self.edge_measured.emit(length, radius)

        elif stype == TopAbs_FACE:
            try:
                props = GProp_GProps()
                brepgprop.SurfaceProperties(shape, props)
                self.face_measured.emit(props.Mass())
            except Exception as e:
                print(f"[measure] face area error: {e}")

    def _add_point_marker(self, pt: gp_Pnt) -> None:
        try:
            ais_pt = AIS_Point(Geom_CartesianPoint(pt))
            ais_pt.Attributes().SetPointAspect(
                Prs3d_PointAspect(Aspect_TOM_BALL, self._measure_color, 8.0)
            )
            self._display.Context.Display(ais_pt, True)
            self._measure_ais.append(ais_pt)
        except Exception as e:
            print(f"[measure] marker error: {e}")

    def _make_dim_aspect(self) -> Prs3d_DimensionAspect:
        asp = Prs3d_DimensionAspect()
        asp.SetCommonColor(self._measure_color)
        return asp

    # ── distance ──────────────────────────────────────────────────────────────

    def _finish_distance(self, p1: gp_Pnt, p2: gp_Pnt) -> None:
        distance = p1.Distance(p2)
        try:
            self._add_length_dimension(p1, p2)
        except Exception as e:
            print(f"[measure] dimension error: {e}")
        self.measurement_done.emit(distance)

    def _add_length_dimension(self, p1: gp_Pnt, p2: gp_Pnt):
        v12 = gp_Vec(p1, p2)
        if v12.Magnitude() < 1e-10:
            return None
        v12n = v12.Normalized()
        ref = gp_Vec(0, 0, 1) if abs(v12n.Z()) < 0.9 else gp_Vec(1, 0, 0)
        normal = v12n.Crossed(ref)
        if normal.Magnitude() < 1e-10:
            normal = gp_Vec(0, 1, 0)
        normal.Normalize()
        dim = PrsDim_LengthDimension(p1, p2, gp_Pln(p1, gp_Dir(normal)))
        dim.SetDimensionAspect(self._make_dim_aspect())
        flyout = max(p1.Distance(p2) * 0.4, self._model_diag * 0.05)
        dim.SetFlyout(flyout)
        self._display.Context.Display(dim, True)
        self._display.Context.SetZLayer(dim, Graphic3d_ZLayerId_Topmost)
        self._measure_ais.append(dim)
        self._dim_objects.append(dim)
        return dim

    # ── angle ─────────────────────────────────────────────────────────────────

    def _finish_angle(self, p1: gp_Pnt, vertex: gp_Pnt, p2: gp_Pnt) -> None:
        v1 = gp_Vec(vertex, p1)
        v2 = gp_Vec(vertex, p2)
        if v1.Magnitude() < 1e-10 or v2.Magnitude() < 1e-10:
            self.measure_cancelled.emit()
            return
        angle_deg = math.degrees(v1.Angle(v2))
        try:
            self._add_angle_dimension(p1, vertex, p2)
        except Exception as e:
            print(f"[measure] angle dimension error: {e}")
        self.angle_done.emit(angle_deg)

    def _add_angle_dimension(self, p1: gp_Pnt, vertex: gp_Pnt, p2: gp_Pnt):
        dim = PrsDim_AngleDimension(p1, vertex, p2)
        dim.SetDimensionAspect(self._make_dim_aspect())
        arm = max(gp_Vec(vertex, p1).Magnitude(), gp_Vec(vertex, p2).Magnitude())
        flyout = max(arm * 0.4, self._model_diag * 0.05)
        dim.SetFlyout(flyout)
        self._display.Context.Display(dim, True)
        self._display.Context.SetZLayer(dim, Graphic3d_ZLayerId_Topmost)
        self._measure_ais.append(dim)
        self._dim_objects.append(dim)
        return dim
