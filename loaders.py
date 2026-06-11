import os
from dataclasses import dataclass, field
from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IGESControl import IGESControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.BRep import BRep_Builder
from OCC.Core.BRepTools import breptools
from OCC.Core.TopoDS import TopoDS_Shape


@dataclass
class ShapeNode:
    name: str
    shape: TopoDS_Shape
    children: list = field(default_factory=list)   # list[ShapeNode]
    ais_shape: object = field(default=None, repr=False)  # set by viewer

    def leaves(self):
        if not self.children:
            yield self
        else:
            for child in self.children:
                yield from child.leaves()


def load_step(path: str) -> TopoDS_Shape:
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)
    if status != IFSelect_RetDone:
        raise RuntimeError(f"Failed to read STEP file: {path}")
    reader.TransferRoots()
    return reader.OneShape()


def load_iges(path: str) -> TopoDS_Shape:
    reader = IGESControl_Reader()
    status = reader.ReadFile(path)
    if status != IFSelect_RetDone:
        raise RuntimeError(f"Failed to read IGES file: {path}")
    reader.TransferRoots()
    return reader.OneShape()


def load_brep(path: str) -> TopoDS_Shape:
    shape = TopoDS_Shape()
    builder = BRep_Builder()
    if not breptools.Read(shape, path, builder):
        raise RuntimeError(f"Failed to read BRep file: {path}")
    return shape


def load_file_tree(path: str) -> ShapeNode:
    """Load a CAD file and return a ShapeNode tree.

    Parts are named "Part 1, Part 2 …" from the compound decomposition.
    STEP assembly hierarchy is preserved one level deep.
    """
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)

    if ext in ('.stp', '.step'):
        shape = load_step(path)
    elif ext in ('.igs', '.iges'):
        shape = load_iges(path)
    elif ext == '.brep':
        shape = load_brep(path)
    else:
        raise ValueError(f"Unsupported format: {ext!r}")

    return _decompose_compound(shape, name)


def _decompose_compound(shape: TopoDS_Shape, name: str) -> ShapeNode:
    from OCC.Core.TopoDS import TopoDS_Iterator
    from OCC.Core.TopAbs import TopAbs_COMPOUND

    if shape.ShapeType() != TopAbs_COMPOUND:
        return ShapeNode(name=name, shape=shape)

    children = []
    it = TopoDS_Iterator(shape)
    idx = 1
    while it.More():
        children.append(ShapeNode(name=f"Part {idx}", shape=it.Value()))
        idx += 1
        it.Next()

    if len(children) <= 1:
        return ShapeNode(name=name, shape=shape)

    return ShapeNode(name=name, shape=shape, children=children)


FILE_FILTER = (
    "CAD Files (*.stp *.step *.igs *.iges *.brep);;"
    "STEP (*.stp *.step);;"
    "IGES (*.igs *.iges);;"
    "BRep (*.brep);;"
    "All Files (*.*)"
)
