"""Parallel hidden-line rendering.

Parts whose screen footprints never touch can't hide each other, so they
are split into groups and drawn in separate worker processes at the same
time. A group that takes too long with the exact method (springs and
threads are the usual cause) is redone with the fast mesh method.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout

from pebble import ProcessExpired, ProcessPool

from .hlr import View, matrix_to_trsf

_pool: ProcessPool | None = None


def _workers() -> int:
    return max(2, min(6, (os.cpu_count() or 4) - 2))


def pool() -> ProcessPool:
    global _pool
    if _pool is None:
        _pool = ProcessPool(max_workers=_workers())
    return _pool


def shutdown():
    global _pool
    if _pool is not None:
        _pool.stop()
        _pool.join()
        _pool = None


# ----------------------------------------------------------------------------
# Worker side
# ----------------------------------------------------------------------------
_shapes: dict = {}


def _load(cache_dir: str, pid: str, angle: float, variant: str = ""):
    # variant: a second copy, so a different mesh size does not replace the usual one
    key = (cache_dir, pid, round(angle, 5), variant)
    shape = _shapes.get(key)
    if shape is None:
        from OCP.BRep import BRep_Builder
        from OCP.BRepLib import BRepLib
        from OCP.BRepTools import BRepTools
        from OCP.TopoDS import TopoDS_Shape

        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, os.path.join(cache_dir, "shapes", f"{pid}.brep"), BRep_Builder()):
            raise RuntimeError(f"Could not read part {pid}")
        if angle > 0:
            BRepLib.EncodeRegularity_s(shape, angle)
        if len(_shapes) > 400:
            _shapes.clear()
        _shapes[key] = shape
    return shape


def _cylinder(base, axis, radius, height, angle):
    from OCP.BRepLib import BRepLib
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*axis)), radius, height).Shape()
    if angle > 0:
        BRepLib.EncodeRegularity_s(shape, angle)
    return shape


def job(cache_dir, items, view, engine, smooth, hidden, angle, coarse=(), proxies=()):
    from .hlr import View as _View
    from .hlr import place, project

    # proxies: (id, base, axis, radius, height) drawn as a plain cylinder instead
    if proxies:
        px = {p[0]: p[1:] for p in proxies}
        shapes = [(pid, _cylinder(*px[pid], angle)) if pid in px else (pid, place(_load(cache_dir, pid, angle), m)) for pid, m in items]
        return project(shapes, _View(*view), engine=engine, smooth=smooth, hidden=hidden, coarse=set(c if isinstance(c, str) else c[0] for c in coarse))

    # coarse: part ids (usual coarse mesh) or (id, deflection, angle) for a mesh of their own
    sizes = {c[0]: (c[1], c[2]) for c in coarse if isinstance(c, (tuple, list))}
    shapes = [(pid, place(_load(cache_dir, pid, angle, "coarse" if pid in sizes else ""), m)) for pid, m in items]
    return project(shapes, _View(*view), engine=engine, smooth=smooth, hidden=hidden, coarse={c if isinstance(c, str) else c[0] for c in coarse}, sizes=sizes)


def probe_job(cache_dir, pid, angle):
    """Time one part on its own with the exact method."""
    from .hlr import View as _View
    from .hlr import project

    t = time.time()
    project([(pid, _load(cache_dir, pid, angle))], _View((0.577, -0.577, 0.577), (0.707, 0.707, 0)), engine="exact")
    return time.time() - t


# ----------------------------------------------------------------------------
# Main side
# ----------------------------------------------------------------------------
_probing: set[str] = set()


def heavy_parts(cache_dir: str) -> set[str]:
    path = os.path.join(cache_dir, "heavy.json")
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return set(json.load(f))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def _complexity(shape):
    """Spline pole count and face count. Threads and springs score high."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_BSplineCurve, GeomAbs_BSplineSurface
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    poles = 0
    faces = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        faces += 1
        try:
            a = BRepAdaptor_Surface(TopoDS.Face(exp.Current()), False)
            if a.GetType() == GeomAbs_BSplineSurface:
                poles += a.NbUPoles() * a.NbVPoles()
        except Exception:  # noqa: BLE001
            pass
        exp.Next()
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        try:
            c = BRepAdaptor_Curve(TopoDS.Edge(exp.Current()))
            if c.GetType() == GeomAbs_BSplineCurve:
                poles += c.NbPoles()
        except Exception:  # noqa: BLE001
            pass
        exp.Next()
    return poles, faces


def probe_model(model, cache_dir: str, limit: float = 2.5):
    """Find parts too slow for the exact method. Runs once per model, in the background.

    Step 1 flags spline-heavy parts at once (threads, springs).
    Step 2 times one copy of every other kind of part.
    """
    path = os.path.join(cache_dir, "heavy.json")
    done_flag = os.path.join(cache_dir, "probe.done")
    if os.path.isfile(done_flag) or cache_dir in _probing:
        return
    _probing.add(cache_dir)

    def save(heavy):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(heavy), f)
        os.replace(tmp, path)

    # Step 1 now (quick), so the first drawing already knows the slow parts.
    heavy: set[str] = set()
    kinds: dict[tuple, list[str]] = {}
    try:
        for part in model.parts:
            poles, faces = _complexity(part.shape)
            if poles >= 4000:
                heavy.add(part.id)
                continue
            x0, y0, z0, x1, y1, z1 = part.bbox
            dims = tuple(sorted(round(v, 1) for v in (x1 - x0, y1 - y0, z1 - z0)))
            kinds.setdefault((part.name.split(" · ")[0], faces, poles, dims), []).append(part.id)
        save(heavy)
    except Exception:  # noqa: BLE001
        _probing.discard(cache_dir)
        raise

    def run():
        # Step 2: time one copy of each other kind of part, in its own small
        # pool so it never blocks a drawing the user asked for.
        p = None
        try:
            p = ProcessPool(max_workers=3)
            angle = math.radians(8.0)
            futs = [(pids, p.schedule(probe_job, args=(cache_dir, pids[0], angle), timeout=limit)) for pids in kinds.values()]
            for pids, fut in futs:
                try:
                    slow = fut.result() > 1.0
                except Exception:  # noqa: BLE001
                    slow = True
                if slow:
                    heavy.update(pids)
                    save(heavy)
            open(done_flag, "w").close()
            print(f"[probe] {len(heavy)} of {len(model.parts)} parts will use the fast method", flush=True)
        finally:
            if p is not None:
                p.stop()
                p.join()
            _probing.discard(cache_dir)

    threading.Thread(target=run, daemon=True).start()


def _bbox2d(bbox, matrix, view: View):
    """Screen-space box of a part's (moved) 3D bounding box."""
    x0, y0, z0, x1, y1, z1 = bbox
    ax = view.ax2()
    xd, yd = ax.XDirection(), ax.YDirection()
    trsf = matrix_to_trsf(matrix) if matrix else None
    xs, ys = [], []
    for x in (x0, x1):
        for y in (y0, y1):
            for z in (z0, z1):
                if trsf is not None:
                    from OCP.gp import gp_Pnt

                    p = gp_Pnt(x, y, z).Transformed(trsf)
                    x_, y_, z_ = p.X(), p.Y(), p.Z()
                else:
                    x_, y_, z_ = x, y, z
                xs.append(x_ * xd.X() + y_ * xd.Y() + z_ * xd.Z())
                ys.append(x_ * yd.X() + y_ * yd.Y() + z_ * yd.Z())
    return (min(xs), min(ys), max(xs), max(ys))


def _clusters(boxes: dict[str, tuple], margin: float = 0.5):
    ids = list(boxes)
    parent = {i: i for i in ids}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, a in enumerate(ids):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in ids[i + 1 :]:
            bx0, by0, bx1, by1 = boxes[b]
            if ax0 - margin <= bx1 and bx0 - margin <= ax1 and ay0 - margin <= by1 and by0 - margin <= ay1:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


_results: "OrderedDict[tuple, dict]" = OrderedDict()


def _remember(key, value):
    _results[key] = value
    _results.move_to_end(key)
    while len(_results) > 600:
        _results.popitem(last=False)


def _tkey(m):
    return tuple(round(v, 3) for v in m) if m else None


def line_job(cache_dir, occluders, lines, view, angle, coarse=()):
    """Visible parts of straight 3D lines, hidden by the given parts."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.gp import gp_Pnt

    from .hlr import View as _View
    from .hlr import place, project

    shapes = [(pid, place(_load(cache_dir, pid, angle), m)) for pid, m in occluders]
    for lid, a, b in lines:
        shapes.append((f"line:{lid}", BRepBuilderAPI_MakeEdge(gp_Pnt(*a), gp_Pnt(*b)).Edge()))
    res = project(shapes, _View(*view), engine="fast", coarse=set(coarse))
    out = {}
    for lid, _a, _b in lines:
        r = res["parts"].get(f"line:{lid}", {})
        out[lid] = r.get("sharp", []) + r.get("outline", [])
    return out


def _clusters_by_outline(sup: dict, margin: float = 0.5):
    """Group parts whose convex outlines (support functions) touch."""
    import numpy as np

    from .layout import OPP

    ids = list(sup)
    if not ids:
        return []
    H = np.array([sup[i] for i in ids])  # (n, K)
    # P and Q are apart if some direction n has max_P(n) < min_Q(n) = -h_Q(-n)
    parent = list(range(len(ids)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(len(ids)):
        gap = H[i][None, :] + H[i + 1 :][:, OPP]  # h_P(n) + h_Q(-n) < 0 -> separated along n
        touching = ~(gap < -margin).any(axis=1)
        for j in np.nonzero(touching)[0]:
            a, b = find(i), find(i + 1 + int(j))
            if a != b:
                parent[a] = b
    groups: dict[int, list[str]] = {}
    for i, pid in enumerate(ids):
        groups.setdefault(find(i), []).append(pid)
    return list(groups.values())


_proxy_cache: dict = {}


def _rod_proxies(model, cache_dir, pids):
    """Long threaded rods: seconds to draw, and in the small assembled view a
    plain cylinder looks the same. Returns (id, base, axis, radius, height)."""
    from .layout import model_geo

    geos = model_geo(model)
    parts = {p.id: p for p in model.parts}
    out = []
    for pid in pids:
        key = (cache_dir, pid)
        if key not in _proxy_cache:
            g = geos.get(pid)
            val = None
            if g is not None and g.ext[0] >= 8 * max(g.ext[1], 1e-6) and _complexity(parts[pid].shape)[1] >= 200:
                a = g.axes[0]
                mid = sum(ax * (float((g.verts @ ax).max()) + float((g.verts @ ax).min())) / 2 for ax in g.axes)
                base = mid - a * g.ext[0] / 2
                val = (pid, tuple(float(v) for v in base), tuple(float(v) for v in a), float(g.ext[1]) / 2, float(g.ext[0]))
            _proxy_cache[key] = val
        if _proxy_cache[key]:
            out.append(_proxy_cache[key])
    return tuple(out)


def render(
    model,
    cache_dir: str,
    transforms: dict,
    visible: list[str],
    view: View,
    engine: str = "exact",
    smooth: bool = False,
    hidden: bool = False,
    smooth_angle_deg: float = 8.0,
    timeout: float = 40.0,
    line_batches=None,
    outlines=None,
    assembled=False,
):
    """Draw parts (and optional assembly lines). Unchanged groups come from cache.

    line_batches: list of (occluder ids, [(line id, start xyz, end xyz), ...]).
    """
    t0 = time.time()
    parts = {p.id: p for p in model.parts}
    visible = [pid for pid in visible if pid in parts]
    if engine == "preview":
        groups = [[pid] for pid in visible]
    elif outlines:
        groups = _clusters_by_outline({pid: outlines[pid] for pid in visible if pid in outlines})
        missing = [pid for pid in visible if pid not in outlines]
        groups += [[pid] for pid in missing]
    else:
        boxes = {pid: _bbox2d(parts[pid].bbox, transforms.get(pid), view) for pid in visible}
        groups = _clusters(boxes)

    def cost(group):
        return sum(os.path.getsize(os.path.join(cache_dir, "shapes", f"{pid}.brep")) for pid in group)

    groups.sort(key=cost, reverse=True)
    angle = math.radians(max(0.0, smooth_angle_deg))
    vargs = (tuple(view.dir), tuple(view.xdir))
    vkey = tuple(round(v, 5) for v in view.dir + view.xdir)
    p = pool()

    heavy = heavy_parts(cache_dir)

    # Isolated parts have identical visible edges after translation. Render
    # them at their original position and reuse those edges while dragging.
    isolated = {g[0] for g in groups if len(g) == 1
                and (transforms.get(g[0]) is None or
                     transforms[g[0]][:12] == [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0])}

    def gkey(group, eng):
        return ("parts", cache_dir, eng, vkey, smooth, hidden, round(angle, 4), tuple(sorted((pid, _tkey(None if pid in isolated else transforms.get(pid))) for pid in group)))

    def submit(group, eng):
        items = [(pid, None if pid in isolated else transforms.get(pid)) for pid in group]
        coarse = tuple(pid for pid in group if pid in heavy)
        return p.schedule(
            job,
            args=(cache_dir, items, vargs, eng, smooth, hidden, angle, coarse),
            timeout=timeout if eng == "exact" else max(timeout * 4, 300),
        )

    pending = []
    fallback: list[str] = []
    out_parts: dict = {}
    cached = 0
    for g in groups:
        eng = "fast" if engine == "preview" else engine
        if engine == "exact" and heavy.intersection(g):
            eng = "fast"
            fallback.extend(g)
        key = gkey(g, eng)
        if key in _results:
            out_parts.update(_results[key]["parts"])
            cached += 1
            continue
        pending.append(("parts", g, eng, key, submit(g, eng)))

    # the whole assembly, put together, as one drawing
    out_asm: dict = {}
    if assembled:
        g = list(visible)
        eng = "fast" if engine != "exact" or heavy.intersection(g) else "exact"
        key = gkey(g, eng)
        if key in _results:
            out_asm = _results[key]["parts"]
        else:
            items = [(pid, None) for pid in g]

            # long threaded rods take seconds to mesh; in the small assembled view a rough mesh is enough
            def diag(pid):
                b = parts[pid].bbox
                return math.dist(b[:3], b[3:])

            proxies = _rod_proxies(model, cache_dir, [pid for pid in g if pid in heavy])
            coarse = tuple((pid, 1.0, 1.0) if diag(pid) > 100 else pid for pid in g if pid in heavy and pid not in {x[0] for x in proxies})
            fut = p.schedule(job, args=(cache_dir, items, vargs, eng, smooth, hidden, angle, coarse, proxies), timeout=max(timeout * 4, 300))
            pending.insert(0, ("asm", g, eng, key, fut))

    out_lines: dict = {}
    for occ, lines in line_batches or []:
        occ_items = [(pid, transforms.get(pid)) for pid in occ if pid not in heavy]
        lines_r = [(lid, tuple(round(v, 3) for v in a), tuple(round(v, 3) for v in b)) for lid, a, b in lines]
        key = ("lines", cache_dir, vkey, round(angle, 4), tuple(sorted((pid, _tkey(m)) for pid, m in occ_items)), tuple(lines_r))
        if key in _results:
            out_lines.update(_results[key])
            continue
        fut = p.schedule(line_job, args=(cache_dir, occ_items, lines_r, vargs, angle, ()), timeout=max(timeout * 4, 300))
        pending.append(("lines", lines_r, "fast", key, fut))

    failed: list[str] = []
    while pending:
        kind, group, eng, key, fut = pending.pop(0)
        try:
            res = fut.result()
            if kind == "lines":
                out_lines.update(res)
                _remember(key, res)
                if os.environ.get("CADRAWS_DEBUG"):
                    print(f"[render] lines {len(group):3d} done at {time.time() - t0:.1f}s occluders={len(key[4])}", flush=True)
                continue
            if kind == "asm":
                out_asm = res["parts"]
                _remember(key, {"parts": res["parts"]})
                if os.environ.get("CADRAWS_DEBUG"):
                    print(f"[render] assembled {eng} {len(group)} parts {res['timing']} done at {time.time() - t0:.1f}s", flush=True)
                continue
            out_parts.update(res["parts"])
            _remember(key, {"parts": res["parts"]})
            if os.environ.get("CADRAWS_DEBUG"):
                names = [parts[pid].name for pid in group][:4]
                print(f"[render] {eng:5s} {len(group):3d} parts {res['timing']} done at {time.time() - t0:.1f}s {names}", flush=True)
        except (FutureTimeout, ProcessExpired, Exception) as e:  # noqa: BLE001
            if kind in ("parts", "asm") and eng == "exact":
                if kind == "parts":
                    fallback.extend(group)
                pending.append((kind, group, "fast", gkey(group, "fast"), submit(group, "fast")))
            else:
                print(f"render {kind} job failed:", type(e).__name__, e, flush=True)
                if kind == "parts":
                    failed.extend(group)

    from .layout import _proj
    _, projection = _proj({"dir": view.dir, "xdir": view.xdir})
    for pid in isolated:
        transform = transforms.get(pid)
        if transform is not None and pid in out_parts:
            import numpy as np
            dx, dy = np.asarray(transform[12:15]) @ projection
            # Copy: cached paths must remain in their original coordinates.
            out_parts[pid] = {kind: [[[x + dx, y + dy] for x, y in poly]
                                     for poly in polys]
                              for kind, polys in out_parts[pid].items()}

    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for res in list(out_parts.values()) + [{"l": v} for v in out_lines.values()]:
        for polys in res.values():
            for poly in polys:
                for x, y in poly:
                    if x < xmin:
                        xmin = x
                    if x > xmax:
                        xmax = x
                    if y < ymin:
                        ymin = y
                    if y > ymax:
                        ymax = y
    if xmin == math.inf:
        xmin = ymin = 0
        xmax = ymax = 1
    return {
        "parts": {pid: out_parts[pid] for pid in visible if pid in out_parts},
        "lines": out_lines,
        "assembled": out_asm,
        "bounds": [xmin, ymin, xmax, ymax],
        "groups": len(groups),
        "cached": cached,
        "fallback": fallback,
        "failed": failed,
        "seconds": round(time.time() - t0, 1),
    }
