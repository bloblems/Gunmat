"""Tessellate parts for the in-browser 3D view.

Packs every part into one binary blob:
  [uint32 json_len][json][pad to 4 bytes][float32/uint32 buffers...]
Positions are stored relative to each part's center so the browser can
rotate parts about their own middle.
"""

from __future__ import annotations

import json
import math
import struct

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS


def _deflection(bbox):
    x0, y0, z0, x1, y1, z1 = bbox
    diag = math.dist((x0, y0, z0), (x1, y1, z1))
    return min(max(diag * 0.002, 0.01), 0.4)


def mesh_part(shape, bbox):
    defl = _deflection(bbox)
    BRepMesh_IncrementalMesh(shape, defl, False, 0.35, True)

    positions = []
    indices = []
    base = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face(exp.Current())
        exp.Next()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        n = tri.NbNodes()
        pts = np.empty((n, 3), dtype=np.float64)
        for i in range(1, n + 1):
            p = tri.Node(i).Transformed(trsf)
            pts[i - 1] = (p.X(), p.Y(), p.Z())
        m = tri.NbTriangles()
        tris = np.empty((m, 3), dtype=np.int64)
        for i in range(1, m + 1):
            tris[i - 1] = tri.Triangle(i).Get()
        tris -= 1
        if face.Orientation() == TopAbs_REVERSED:
            tris = tris[:, [0, 2, 1]]
        positions.append(pts)
        indices.append(tris + base)
        base += n

    edges = []
    emap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, emap)
    for i in range(1, emap.Extent() + 1):
        edge = TopoDS.Edge(emap.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):
            continue
        try:
            curve = BRepAdaptor_Curve(edge)
            disc = GCPnts_TangentialDeflection(curve, 0.2, defl)
            pts = [disc.Value(j) for j in range(1, disc.NbPoints() + 1)]
        except Exception:
            continue
        for a, b in zip(pts, pts[1:]):
            edges.append((a.X(), a.Y(), a.Z(), b.X(), b.Y(), b.Z()))

    pos = np.concatenate(positions) if positions else np.zeros((0, 3))
    idx = np.concatenate(indices) if indices else np.zeros((0, 3), dtype=np.int64)
    edg = np.array(edges, dtype=np.float64).reshape(-1, 3) if edges else np.zeros((0, 3))
    return pos, idx, edg


def pack_meshes(parts) -> bytes:
    """parts: list of step_loader.Part. Returns the packed blob."""
    meta = []
    chunks = []
    offset = 0

    def add(arr: np.ndarray):
        nonlocal offset
        b = arr.tobytes()
        start = offset
        chunks.append(b)
        offset += len(b)
        return start

    for p in parts:
        pos, idx, edg = mesh_part(p.shape, p.bbox)
        c = np.array(p.center)
        pos32 = (pos - c).astype(np.float32) if len(pos) else pos.astype(np.float32)
        edg32 = (edg - c).astype(np.float32) if len(edg) else edg.astype(np.float32)
        meta.append(
            {
                "id": p.id,
                "pos": [add(pos32), int(pos32.size)],
                "idx": [add(idx.astype(np.uint32)), int(idx.size)],
                "edges": [add(edg32), int(edg32.size)],
            }
        )

    head = json.dumps({"parts": meta}).encode()
    pad = (4 - (4 + len(head)) % 4) % 4
    head += b" " * pad
    return struct.pack("<I", len(head)) + head + b"".join(chunks)
