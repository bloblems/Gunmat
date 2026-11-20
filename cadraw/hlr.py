"""Hidden-line projection of placed parts into flat 2D vector paths.

Uses OpenCascade's HLR. Two engines:
  * "exact" (HLRBRep_Algo) works on the true B-rep. Clean curves, slower.
  * "fast"  (HLRBRep_PolyAlgo) works on the mesh. Much faster, a bit coarser.

Output coordinates are in model millimetres on the view plane, with Y pointing
down (SVG convention).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepLib import BRepLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_Line
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import (
    HLRBRep_Algo,
    HLRBRep_HLRToShape,
    HLRBRep_PolyAlgo,
    HLRBRep_PolyHLRToShape,
)
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf

KINDS = ("sharp", "outline", "smooth", "hidden")


@dataclass
class View:
    """Orthographic view. `dir` points from the model toward the viewer."""

    dir: tuple[float, float, float]
    xdir: tuple[float, float, float]

    def ax2(self) -> gp_Ax2:
        d = _norm(self.dir)
        x = _norm(self.xdir)
        # Make xdir exactly perpendicular to dir.
        dot = sum(a * b for a, b in zip(d, x))
        x = _norm(tuple(a - dot * b for a, b in zip(x, d)))
        return gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*d), gp_Dir(*x))


def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def matrix_to_trsf(m: list[float] | None) -> gp_Trsf:
    """Column-major 4x4 (three.js `Matrix4.elements`) to gp_Trsf."""
    t = gp_Trsf()
    if not m:
        return t
    t.SetValues(
        m[0], m[4], m[8], m[12],
        m[1], m[5], m[9], m[13],
        m[2], m[6], m[10], m[14],
    )
    return t


def place(shape: TopoDS_Shape, matrix: list[float] | None) -> TopoDS_Shape:
    if not matrix:
        return shape
    trsf = matrix_to_trsf(matrix)
    return shape.Moved(TopLoc_Location(trsf))


# ----------------------------------------------------------------------------
def _edges(shape: TopoDS_Shape):
    if shape is None or shape.IsNull():
        return
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        yield TopoDS.Edge(exp.Current())
        exp.Next()


def _edge_points(edge, deflection: float):
    try:
        curve = BRepAdaptor_Curve(edge)
    except Exception:
        return None
    u0, u1 = curve.FirstParameter(), curve.LastParameter()
    if curve.GetType() == GeomAbs_Line:
        p0, p1 = curve.Value(u0), curve.Value(u1)
        return [(p0.X(), p0.Y()), (p1.X(), p1.Y())]
    try:
        disc = GCPnts_QuasiUniformDeflection(curve, deflection, u0, u1)
        if not disc.IsDone() or disc.NbPoints() < 2:
            raise RuntimeError
        pts = []
        for i in range(1, disc.NbPoints() + 1):
            p = disc.Value(i)
            pts.append((p.X(), p.Y()))
        return pts
    except Exception:
        n = 16
        pts = []
        for i in range(n + 1):
            p = curve.Value(u0 + (u1 - u0) * i / n)
            pts.append((p.X(), p.Y()))
        return pts


def _compound_to_polylines(compound, deflection: float, build_3d: bool):
    out = []
    if compound is None or compound.IsNull():
        return out
    if build_3d:
        BRepLib.BuildCurves3d_s(compound, 1e-4)
    for e in _edges(compound):
        pts = _edge_points(e, deflection)
        if not pts or len(pts) < 2:
            continue
        # Flip Y for SVG.
        poly = [(round(x, 3), round(-y, 3)) for x, y in pts]
        if _length(poly) < 1e-3:
            continue
        out.append(poly)
    return out


def _length(poly):
    return sum(math.dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def _join_polylines(polys, tol=2e-3):
    """Chain polylines that share end points. Smaller SVG, cleaner corners."""
    if len(polys) < 2:
        return polys

    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))

    # endpoint key -> set of polyline indices touching it
    ends: dict[tuple, set[int]] = {}
    for i, p in enumerate(polys):
        ends.setdefault(key(p[0]), set()).add(i)
        ends.setdefault(key(p[-1]), set()).add(i)

    used = [False] * len(polys)

    def take(k, exclude):
        for j in ends.get(k, ()):
            if not used[j] and j != exclude:
                return j
        return None

    out = []
    for i in range(len(polys)):
        if used[i]:
            continue
        used[i] = True
        chain = list(polys[i])
        # grow forward
        while True:
            j = take(key(chain[-1]), i)
            if j is None:
                break
            used[j] = True
            p = polys[j]
            chain.extend(p[1:] if key(p[0]) == key(chain[-1]) else list(reversed(p))[1:])
        # grow backward
        while True:
            j = take(key(chain[0]), i)
            if j is None:
                break
            used[j] = True
            p = polys[j]
            chain[:0] = p[:-1] if key(p[-1]) == key(chain[0]) else list(reversed(p))[:-1]
        out.append(_simplify(chain))
    return out


def _simplify(poly, eps=1e-3):
    """Drop points that sit on a straight line between their neighbours."""
    if len(poly) < 3:
        return poly
    out = [poly[0]]
    for i in range(1, len(poly) - 1):
        ax, ay = out[-1]
        bx, by = poly[i]
        cx, cy = poly[i + 1]
        cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        seg = math.hypot(cx - ax, cy - ay)
        if seg > 0 and abs(cross) / seg < eps and (bx - ax) * (cx - bx) + (by - ay) * (cy - by) >= 0:
            continue
        out.append(poly[i])
    out.append(poly[-1])
    return out


# ----------------------------------------------------------------------------
def project(
    items: list[tuple[str, TopoDS_Shape]],
    view: View,
    engine: str = "exact",
    smooth: bool = False,
    hidden: bool = False,
    deflection: float = 0.02,
    mesh_deflection: float = 0.06,
    coarse: set | None = None,
    sizes: dict | None = None,
):
    """Project parts. Returns {"parts": {id: {kind: [polyline, ...]}}, "bounds": [...]}."""
    t0 = time.time()
    projector = HLRAlgo_Projector(view.ax2())

    if engine == "fast":
        coarse = coarse or set()
        sizes = sizes or {}
        for pid, shape in items:
            if pid in sizes:
                BRepMesh_IncrementalMesh(shape, sizes[pid][0], False, sizes[pid][1], True)
            elif pid in coarse:
                # springs and long threads: a finer mesh costs minutes and looks the same at mat size
                BRepMesh_IncrementalMesh(shape, 0.25, False, 0.6, True)
            else:
                BRepMesh_IncrementalMesh(shape, mesh_deflection, False, 0.35, True)
        algo = HLRBRep_PolyAlgo()
        for _, shape in items:
            algo.Load(shape)
        algo.Projector(projector)
        algo.Update()
        to_shape = HLRBRep_PolyHLRToShape()
        to_shape.Update(algo)
        build_3d = False
    else:
        algo = HLRBRep_Algo()
        for _, shape in items:
            algo.Add(shape, 0)
        algo.Projector(projector)
        algo.Update()
        algo.Hide()
        to_shape = HLRBRep_HLRToShape(algo)
        build_3d = True

    t1 = time.time()
    parts = {}
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for pid, shape in items:
        res = {}
        res["sharp"] = _compound_to_polylines(to_shape.VCompound(shape), deflection, build_3d)
        res["outline"] = _compound_to_polylines(to_shape.OutLineVCompound(shape), deflection, build_3d)
        if smooth:
            res["smooth"] = _compound_to_polylines(to_shape.Rg1LineVCompound(shape), deflection, build_3d)
        if hidden:
            h = _compound_to_polylines(to_shape.HCompound(shape), deflection, build_3d)
            h += _compound_to_polylines(to_shape.OutLineHCompound(shape), deflection, build_3d)
            res["hidden"] = h
        for k in list(res):
            res[k] = _join_polylines(res[k])
        for polys in res.values():
            for poly in polys:
                for x, y in poly:
                    xmin, xmax = min(xmin, x), max(xmax, x)
                    ymin, ymax = min(ymin, y), max(ymax, y)
        parts[pid] = res

    if xmin == math.inf:
        xmin = ymin = 0
        xmax = ymax = 1
    return {
        "parts": parts,
        "bounds": [xmin, ymin, xmax, ymax],
        "timing": {"hlr": round(t1 - t0, 2), "extract": round(time.time() - t1, 2)},
    }


def project_point(view: View, p) -> tuple[float, float]:
    """Project a 3D point the same way `project` does (for callout anchors)."""
    ax = view.ax2()
    o = ax.Location()
    x = ax.XDirection()
    y = ax.YDirection()
    v = (p[0] - o.X(), p[1] - o.Y(), p[2] - o.Z())
    px = v[0] * x.X() + v[1] * x.Y() + v[2] * x.Z()
    py = v[0] * y.X() + v[1] * y.Y() + v[2] * y.Z()
    return (px, -py)
