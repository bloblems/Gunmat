"""Read a STEP assembly into a flat list of placed parts.

Each leaf of the assembly tree becomes one Part with its world placement
baked in, a readable name, its assembly path, and a display color.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFDoc import (
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_DocumentTool,
)


@dataclass
class Part:
    id: str
    name: str
    path: list[str]
    shape: TopoDS_Shape  # world placed
    color: tuple[float, float, float] | None
    bbox: tuple[float, float, float, float, float, float] = field(default=(0, 0, 0, 0, 0, 0))

    @property
    def center(self):
        x0, y0, z0, x1, y1, z1 = self.bbox
        return ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)


# Fusion and other exporters often name things with timestamps or
# "Name v12:1" style suffixes. Make names readable.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}")
_INSTANCE_SUFFIX = re.compile(r"(:\d+)$")
_VERSION_SUFFIX = re.compile(r"\s+v\d+$")


def clean_name(raw: str) -> str:
    name = raw.strip()
    name = _INSTANCE_SUFFIX.sub("", name)
    name = _VERSION_SUFFIX.sub("", name)
    # Fusion adds "(1)", "(2) (1)" when copies are made. Strip trailing groups.
    name = re.sub(r"(\s*\(\d+\))+$", "", name)
    return name.strip() or raw.strip()


def _label_name(label: TDF_Label) -> str:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def _has_geometry(shape: TopoDS_Shape) -> bool:
    for kind in (TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE):
        if TopExp_Explorer(shape, kind).More():
            return True
    return False


def _solids(shape: TopoDS_Shape):
    out = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


def _bbox(shape: TopoDS_Shape):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, True)
    if box.IsVoid():
        return (0, 0, 0, 0, 0, 0)
    a, b = box.CornerMin(), box.CornerMax()
    return (a.X(), a.Y(), a.Z(), b.X(), b.Y(), b.Z())


class StepAssembly:
    def __init__(self, path: str):
        self.path = path
        self.parts: list[Part] = []
        self._read()

    # ------------------------------------------------------------------
    def _read(self):
        doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        reader.SetLayerMode(False)
        reader.SetPropsMode(False)
        status = reader.ReadFile(self.path)
        if status != IFSelect_RetDone:
            raise ValueError(f"Could not read STEP file: {self.path}")
        if not reader.Transfer(doc):
            raise ValueError("STEP transfer failed")

        main = doc.Main()
        self._shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(main)
        self._color_tool = XCAFDoc_DocumentTool.ColorTool_s(main)

        free = TDF_LabelSequence()
        self._shape_tool.GetFreeShapes(free)
        self._seen_ids: dict[str, int] = {}
        for i in range(1, free.Length() + 1):
            label = free.Value(i)
            self._walk(label, TopLoc_Location(), [], None)

        self._doc = doc  # keep alive

    def _color_of(self, label: TDF_Label, shape: TopoDS_Shape | None = None):
        c = Quantity_Color()
        for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
            if self._color_tool.GetColor_s(label, kind, c):
                return (c.Red(), c.Green(), c.Blue())
        if shape is not None:
            for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
                if self._color_tool.GetColor(shape, kind, c):
                    return (c.Red(), c.Green(), c.Blue())
        return None

    def _walk(self, label: TDF_Label, loc: TopLoc_Location, path: list[str], color):
        st = self._shape_tool
        name = _label_name(label)
        color = self._color_of(label) or color

        if st.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            st.GetComponents_s(label, comps, False)
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                comp_loc = st.GetLocation_s(comp)
                ref = TDF_Label()
                if not st.GetReferredShape_s(comp, ref):
                    continue
                inst_name = _label_name(comp)
                comp_color = self._color_of(comp) or color
                self._walk(ref, loc.Multiplied(comp_loc), path + [clean_name(inst_name or name)], comp_color)
            return

        shape = st.GetShape_s(label)
        if shape.IsNull() or not _has_geometry(shape):
            return
        color = color or self._color_of(label, shape)
        placed = shape.Moved(loc) if not loc.IsIdentity() else shape

        part_name = clean_name(name) if name else (path[-1] if path else "Part")
        if path and _TIMESTAMP.match(part_name):
            part_name = path[-1]
        full_path = path if (path and path[-1] == part_name) else path + [part_name]

        # A leaf holding several separate solids (common when bodies are
        # exported without an assembly) becomes one part per solid, so each
        # can be moved on its own.
        solids = _solids(placed)
        pieces = [(placed, part_name)] if len(solids) < 2 else [(sol, f"{part_name} · {i + 1}") for i, sol in enumerate(solids)]
        for shape_i, name_i in pieces:
            key = "/".join(full_path[:-1] + [name_i])
            n = self._seen_ids.get(key, 0)
            self._seen_ids[key] = n + 1
            pid = hashlib.sha1(f"{key}#{n}".encode()).hexdigest()[:10]
            self.parts.append(
                Part(
                    id=pid,
                    name=name_i,
                    path=full_path[:-1] + [name_i] if len(pieces) == 1 else full_path + [name_i],
                    shape=shape_i,
                    color=color,
                    bbox=_bbox(shape_i),
                )
            )

    # ------------------------------------------------------------------
    def part(self, pid: str) -> Part:
        for p in self.parts:
            if p.id == pid:
                return p
        raise KeyError(pid)

    @property
    def bbox(self):
        xs0, ys0, zs0, xs1, ys1, zs1 = zip(*(p.bbox for p in self.parts)) if self.parts else ((0,),) * 6
        return (min(xs0), min(ys0), min(zs0), max(xs1), max(ys1), max(zs1))


def load_step(path: str) -> StepAssembly:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return StepAssembly(path)
