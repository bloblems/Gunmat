"""Exploded views with hierarchical geometry and joint sheet-space placement.

Parts separate along natural axes inside their STEP groups. Group positions
are then solved together against their actual projected outlines, sheet
boundaries, and reserved boxes. Large empty regions and attachment lengths
are scored alongside scale. No model-specific coordinates are prescribed.

Convex outlines use support functions in K fixed directions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct

import numpy as np

K = 64
GAP = 0.028  # space between parts, as a share of the main part's size (times Spread)
_ANG = np.linspace(0.0, 2.0 * math.pi, K, endpoint=False)
NORMALS = np.stack([np.cos(_ANG), np.sin(_ANG)], axis=1)  # (K, 2)
OPP = (np.arange(K) + K // 2) % K
WORLD = [np.array(v, float) for v in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]

_geo_cache: dict[str, dict] = {}
_joint_cache: dict = {}


class Geo:
    __slots__ = ("axes", "bbox", "center", "ext", "id", "kind", "main", "parts", "projection", "verts", "vol")


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def _snap(axis):
    for w in WORLD:
        if abs(float(axis @ w)) > math.cos(math.radians(12)):
            return w * (1.0 if axis @ w > 0 else -1.0)
    return _unit(axis)


def model_geo(model) -> dict[str, Geo]:
    """Per-part points, size and main axes. Computed once per model."""
    if model.id in _geo_cache:
        return _geo_cache[model.id]
    blob = model.mesh
    n = struct.unpack_from("<I", blob, 0)[0]
    head = json.loads(blob[4 : 4 + n])
    base = 4 + n
    parts = {p.id: p for p in model.parts}
    rng = np.random.default_rng(0)
    out = {}
    for m in head["parts"]:
        p = parts.get(m["id"])
        if p is None:
            continue
        off, cnt = m["pos"]
        v = np.frombuffer(blob, dtype=np.float32, count=cnt, offset=base + off).reshape(-1, 3).astype(np.float64)
        c = np.array(p.center, float)
        if len(v) == 0:
            x0, y0, z0, x1, y1, z1 = p.bbox
            v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], float) - c
        if len(v) > 6000:
            v = v[rng.choice(len(v), 6000, replace=False)]
        g = Geo()
        g.id = p.id
        g.center = c
        g.verts = v + c
        x0, y0, z0, x1, y1, z1 = p.bbox
        g.bbox = np.array(p.bbox, float)
        g.vol = max(x1 - x0, 1e-3) * max(y1 - y0, 1e-3) * max(z1 - z0, 1e-3)
        # main axes
        cov = np.cov(v.T) if len(v) > 3 else np.eye(3)
        _, vecs = np.linalg.eigh(cov)
        axes = [_snap(vecs[:, i]) for i in (2, 1, 0)]
        ext = [float(np.ptp(v @ a)) for a in axes]
        order = np.argsort(ext)[::-1]
        g.axes = [axes[i] for i in order]
        g.ext = [ext[i] for i in order]
        e0, e1, e2 = g.ext
        if e0 > 1.8 * max(e1, 1e-6):
            g.kind = "long"
        elif e2 < 0.45 * max(e1, 1e-6):
            g.kind = "flat"
        else:
            g.kind = "block"
        out[p.id] = g
    _geo_cache[model.id] = out
    return out


# ----------------------------------------------------------------------------
def _proj(view):
    d = _unit(np.array(view["dir"], float))
    x = np.array(view["xdir"], float)
    x = _unit(x - (x @ d) * d)
    y = np.cross(d, x)
    # drawing coords: (x, -y) to match the hidden-line output (SVG y down)
    M = np.stack([x, -y], axis=1)  # (3, 2)
    return d, M


def _support(pts2):
    return (pts2 @ NORMALS.T).max(axis=0)


def _free_distance(Hp, u, placed, gap, s_min=0.0):
    """Smallest s >= s_min so a part moved by s*u touches nothing in `placed`.

    Hp is one outline (K,) or several (n, K) that move together (a whole
    sub-assembly, each part its own outline, so others can sit in its gaps).
    """
    if not len(placed):
        return s_min
    S = np.asarray(placed)  # (m, K)
    Hp = np.atleast_2d(Hp)  # (n, K)
    C = (S[None, :, :] + Hp[:, None, OPP] + gap).reshape(-1, K)  # every outline pair: overlap needs nu_k * s <= C_k
    nu = NORMALS @ u  # (K,)
    pos = nu > 1e-9
    neg = nu < -1e-9
    zero = ~(pos | neg)
    with np.errstate(divide="ignore", invalid="ignore"):
        R = C / nu[None, :]
    upper = np.where(pos[None, :], R, np.inf).min(axis=1)
    lower = np.where(neg[None, :], R, -np.inf).max(axis=1)
    never = (np.where(zero[None, :], C, 1.0) < 0).any(axis=1)
    ok = (lower < upper) & ~never & (upper > s_min)
    iv = sorted(zip(lower[ok], upper[ok]))
    s = s_min
    changed = True
    while changed:
        changed = False
        for lo, up in iv:
            if lo <= s < up:
                s = up
                changed = True
    return float(s)


def _spread_distance(outline, direction, placed, ref, spread):
    """Find the nearest clear position using the requested part spacing."""
    return _free_distance(outline, direction, placed, GAP * ref * spread)


def _contact_distance(Hp, u, placed, gap):
    """How far a free part (support Hp) can move along u before it comes
    within `gap` of anything in `placed`. inf if nothing is in the way."""
    if not len(placed):
        return math.inf
    S = np.asarray(placed)
    C = S + Hp[OPP][None, :] + gap
    nu = NORMALS @ u
    pos, neg = nu > 1e-9, nu < -1e-9
    with np.errstate(divide="ignore", invalid="ignore"):
        R = C / nu[None, :]
    upper = np.where(pos[None, :], R, np.inf).min(axis=1)
    lower = np.where(neg[None, :], R, -np.inf).max(axis=1)
    never = (np.where(~(pos | neg)[None, :], C, 1.0) < 0).any(axis=1)
    ok = (lower < upper) & ~never & (upper > 0)
    if not ok.any():
        return math.inf
    return float(max(0.0, lower[ok].min()))


def _candidates(g: Geo):
    cands = []
    if g.kind == "long":
        cands.append((g.axes[0], 0.35))
    elif g.kind == "flat":
        cands.append((g.axes[2], 0.35))
    for w in WORLD:
        cands.append((w, 0.0))
    if g.kind == "block" and not any(abs(abs(g.axes[0] @ w) - 1) < 1e-6 for w in WORLD):
        cands.append((g.axes[0], 0.1))
    out = []
    for a, bonus in cands:
        for sgn in (1.0, -1.0):
            d = a * sgn
            if any(float(d @ e) > 0.995 for e, _ in out):
                continue
            out.append((d, bonus))
    return out


def _parents(geos, ids, root):
    """Smallest clearly-bigger part whose box holds each part's centre."""
    parent = {}
    for pid in ids:
        if pid == root:
            continue
        g = geos[pid]
        best = None
        for qid in ids:
            if qid == pid or qid == root:
                continue
            q = geos[qid]
            if q.vol < 2.5 * g.vol:
                continue
            b = q.bbox
            if (np.all(g.center >= b[:3]) and np.all(g.center <= b[3:])
                    and (best is None or q.vol < geos[best].vol)):
                best = qid
        parent[pid] = best or root
    return parent


def _bundles(geos, ids):
    """Parallel rods packed side by side (for example, a row of pins).

    They stay together and keep their arrangement when exploded.
    """
    rods = [p for p in ids if ":" not in p and geos[p].kind == "long" and geos[p].ext[0] > 4 * max(geos[p].ext[1], 1e-6)]
    root = {p: p for p in rods}

    def find(a):
        while root[a] != a:
            root[a] = root[root[a]]
            a = root[a]
        return a

    for i, a in enumerate(rods):
        ga = geos[a]
        ax = ga.axes[0]
        for b in rods[i + 1 :]:
            gb = geos[b]
            if abs(float(ax @ gb.axes[0])) < 0.99:
                continue
            if abs(ga.ext[0] - gb.ext[0]) > 0.08 * max(ga.ext[0], gb.ext[0]):
                continue
            d = gb.center - ga.center
            along = float(d @ ax)
            if abs(along) > 0.1 * ga.ext[0]:
                continue
            if np.linalg.norm(d - along * ax) > 0.75 * (ga.ext[1] + gb.ext[1]):
                continue
            root[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for p in rods:
        groups.setdefault(find(p), []).append(p)
    return [g for g in groups.values() if len(g) >= 2]


def _bundle_unit(geos, members, open_up=1.5):
    """One layout unit for a bundle. The arrangement is opened up a little
    (around the bundle axis) so each rod can be seen."""
    gs = [geos[m] for m in members]
    ax = gs[0].axes[0]
    c = np.mean([g.center for g in gs], axis=0)
    inner = {}
    for g in gs:
        d = g.center - c
        inner[g.id] = (d - (d @ ax) * ax) * (open_up - 1.0)
    verts = np.concatenate([g.verts + inner[g.id] for g in gs])
    if len(verts) > 6000:
        verts = verts[np.random.default_rng(0).choice(len(verts), 6000, replace=False)]
    u = Geo()
    u.id = "bundle:" + members[0]
    u.verts = verts
    u.center = c
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    u.bbox = np.concatenate([lo, hi])
    u.vol = float(np.prod(np.maximum(hi - lo, 1e-3)))
    u.axes, u.ext, u.kind = gs[0].axes, gs[0].ext, gs[0].kind
    return u, inner


def _outlines(g, M):
    """Outlines of a unit: one per part for a sub-assembly, else one."""
    key = tuple(M.ravel())
    cached = getattr(g, "projection", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    parts = getattr(g, "parts", None)
    if parts:
        result = np.stack([_support(v @ M) for v in parts])
    else:
        result = _support(g.verts @ M)[None, :]
    g.projection = (key, result)
    return result


def _run(geos, ids, root, parent, pinned, view, spread, aspect, bias, asm=None, ref=None, walls=(), box=None, axis=None, guides=None):
    d_view, M = _proj(view)
    pts2 = {pid: geos[pid].verts @ M for pid in ids}
    H0 = {pid: _outlines(geos[pid], M) for pid in ids}  # (n, K): one outline per part in it
    r2 = pts2[root]
    ref = ref or float(np.hypot(*np.ptp(r2, axis=0))) or 1.0
    gap = GAP * ref * spread

    offsets = {root: np.zeros(3)}
    placed = list(H0[root]) + list(walls)  # walls: no-go boxes (logos, assembled view)
    for pid, off in pinned.items():
        if pid in H0 and pid != root:
            o = np.array(off, float)
            offsets[pid] = o
            placed.extend(H0[pid] + NORMALS @ (o @ M))

    # order: by depth in the parent tree, then from inside out
    def depth(pid):
        n = 0
        while pid in parent and pid != root and n < 20:
            pid = parent[pid]
            n += 1
        return n

    todo = [pid for pid in ids if pid != root and pid not in offsets]
    def directions(pid):
        if guides and pid in guides:
            return [(guides[pid], 0.0)]
        return _candidates(geos[pid])

    chosen_dir = {}
    for pid in todo:
        g = geos[pid]
        pc = geos[parent[pid]].center
        rel = _unit(g.center - pc)
        best = None
        for d, bonus in directions(pid):
            v2 = d @ M
            f = float(np.hypot(*v2))
            if f < 0.3:
                continue
            score = -bonus - 0.35 * float(d @ rel) - 0.1 * (1 if d @ d_view > 0 else 0) - 0.25 * f
            if best is None or score < best[0]:
                best = (score, d)
        chosen_dir[pid] = best[1] if best else None
    todo.sort(key=lambda p: (depth(p), float(np.max((geos[p].verts - geos[parent[p]].center) @ chosen_dir[p])) if chosen_dir[p] is not None else 0.0))

    def room(Hb, u):
        """How far a part can move along u and stay inside `box`."""
        if box is None:
            return math.inf
        x0, y0, x1, y1 = box
        lim = math.inf
        right, left = Hb[:, 0].max(), -Hb[:, K // 2].max()
        low, high = Hb[:, K // 4].max(), -Hb[:, 3 * K // 4].max()
        if u[0] > 1e-9:
            lim = min(lim, (x1 - right) / u[0])
        if u[0] < -1e-9:
            lim = min(lim, (x0 - left) / u[0])
        if u[1] > 1e-9:
            lim = min(lim, (y1 - low) / u[1])
        if u[1] < -1e-9:
            lim = min(lim, (y0 - high) / u[1])
        return lim

    def keep_order(g, dvec, dist):
        """Cost for breaking front-to-back order along the assembly: parts explode
        out from where they sit; moving toward or past the middle costs a lot."""
        if axis is None:
            return 0.0, 0.0
        a, tc = axis
        m = float(dvec @ a) * dist
        side = float(np.sign(float(g.center @ a) - tc))
        back = max(0.0, -side * m) if side else 0.0
        return (1.0 * abs(m) + 2.5 * back) / ref, max(back, abs(m) * 0.35)

    # when the area edges force a choice, a move that breaks the order doesn't count as fitting
    order_limit = 0.1 * ref

    lines = {}
    failed = False
    for pid in todo:
        g = geos[pid]
        par = parent[pid]
        base = offsets.get(par, np.zeros(3))
        Hb = H0[pid] + NORMALS @ (base @ M)
        rel = _unit(g.center - geos[par].center)
        best = loose = None
        for d, bonus in directions(pid):
            v2 = d @ M
            f = float(np.hypot(*v2))
            if f < 0.3:
                continue
            u = v2 / f
            s = _spread_distance(Hb, u, placed, ref, spread)
            pref = -bonus - 0.35 * float(d @ rel) - 0.1 * (1 if d @ d_view > 0 else 0) + 0.25 * (1 - f)
            if bias:
                pref -= abs(bias) * (abs(u[1]) if bias > 0 else abs(u[0]))
            pen, back = keep_order(g, d, s / f)
            cost = s / ref + pref + pen
            if loose is None or cost < loose[0]:
                loose = (cost, d, s, f)
            if s <= room(Hb, u) + 1e-6 and (box is None or back <= order_limit) and (best is None or cost < best[0]):
                best = (cost, d, s, f)
        if best is None and box is not None:
            # Try screen-plane moves only when natural directions cannot fit.
            for a in np.linspace(0, 2 * math.pi, 32, endpoint=False):
                u = np.array([math.cos(a), math.sin(a)])
                s = _spread_distance(Hb, u, placed, ref, spread)
                pen, back = keep_order(g, M @ u, s)
                cost = s / ref + pen
                if s <= room(Hb, u) + 1e-6 and back <= order_limit and (best is None or cost < best[0]):
                    best = (cost, M @ u, s, 1.0)
            if best is None:
                failed = True
        best = best or loose
        if best is None:
            # nothing moves visibly; push straight out on screen
            v2 = (g.center - geos[par].center) @ M
            u = _unit(v2) if np.linalg.norm(v2) > 1e-6 else np.array([0.0, -1.0])
            s = _spread_distance(Hb, u, placed, ref, spread)
            dvec = M @ u  # back to 3D in the view plane
            dvec = _unit(dvec)
            off = base + dvec * s
        else:
            _, d, s, f = best
            dvec = d
            off = base + d * (s / f)
        offsets[pid] = off
        placed.extend(H0[pid] + NORMALS @ (off @ M))
        if np.linalg.norm(off - base) > 1e-6:
            # line from where the part sits to the near face of the moved part
            back = float(np.max((g.verts - g.center) @ (-dvec)))
            start = g.center + base
            end = g.center + off - dvec * back
            if (end - start) @ dvec > 0.5:
                lines[pid] = (start, end)

    # parts placed by hand (or kept from the last layout) get their line too
    for pid, off in offsets.items():
        if pid == root or pid in lines:
            continue
        g = geos[pid]
        base = offsets.get(parent.get(pid, root), np.zeros(3))
        mv = off - base
        n = float(np.linalg.norm(mv))
        if n < 1e-6:
            continue
        dvec = mv / n
        back = float(np.max((g.verts - g.center) @ (-dvec)))
        start = g.center + base
        end = g.center + off - dvec * back
        if (end - start) @ dvec > 0.5:
            lines[pid] = (start, end)

    def fit_of(stack):
        allS = np.array(stack)
        xmin, xmax = -allS[:, K // 2].max(), allS[:, 0].max()
        ymin, ymax = -allS[:, 3 * K // 4].max(), allS[:, K // 4].max()
        bw, bh = xmax - xmin, ymax - ymin
        return min(aspect / max(bw, 1e-6), 1.0 / max(bh, 1e-6)), (bw, bh)

    # the whole assembly, put together, in free space next to the explode
    asm_out = None
    if asm is not None:
        Ha, scale, pin = asm
        c2 = np.array([(Ha[0] - Ha[K // 2]) / 2, (Ha[K // 4] - Ha[3 * K // 4]) / 2])
        Hs = scale * Ha + (1.0 - scale) * (NORMALS @ c2)
        if pin is not None:
            off2 = np.array(pin, float)
        else:
            # start just outside the explode (below, above, right, left), then
            # slide in partway; keep the spot that fills the drawing area best
            allS = np.array(placed)
            fx0, fx1 = -allS[:, K // 2].max(), allS[:, 0].max()
            fy0, fy1 = -allS[:, 3 * K // 4].max(), allS[:, K // 4].max()
            ax0, ax1, ay0, ay1 = -Hs[K // 2], Hs[0], -Hs[3 * K // 4], Hs[K // 4]
            sep = 3 * gap
            xs = (fx0 - ax0, (fx0 + fx1 - ax0 - ax1) / 2, fx1 - ax1)
            ys = (fy0 - ay0, (fy0 + fy1 - ay0 - ay1) / 2, fy1 - ay1)
            cands = [((x, fy1 + sep - ay0), (0.0, -1.0), ay1 - ay0) for x in xs]  # below
            cands += [((x, fy0 - sep - ay1), (0.0, 1.0), ay1 - ay0) for x in xs]  # above
            cands += [((fx1 + sep - ax0, y), (-1.0, 0.0), ax1 - ax0) for y in ys]  # right
            cands += [((fx0 - sep - ax1, y), (1.0, 0.0), ax1 - ax0) for y in ys]  # left
            best = None
            for start, u, size in cands:
                start, u = np.array(start), np.array(u)
                c = _contact_distance(Hs + NORMALS @ start, u, placed, sep)
                off = start + u * min(c if math.isfinite(c) else 0.0, 0.35 * size)
                f, _ = fit_of(placed + [Hs + NORMALS @ off])
                if best is None or f > best[0] * 1.001:
                    best = (f, off)
            off2 = best[1]
        placed.append(Hs + NORMALS @ off2)
        asm_out = {"offset": off2, "center": c2, "scale": scale}

    fit, size = fit_of(placed)
    return offsets, lines, (-1.0 if failed else fit), size, asm_out


_HW = re.compile(r"\b(hardware|fasteners?|screws|bolts|nuts|misc|purchased( parts)?)\b", re.IGNORECASE)


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def step_groups(model, ids):
    """Top-level sub-assemblies from the STEP file: {name: [part ids]}.

    Folders named like "Hardware / Housing Hardware" join the "Housing"
    group. Other hardware folders don't make a group (their parts go alone).
    """
    want = set(ids)
    parts = [p for p in model.parts if p.id in want]
    paths = [list(p.path[:-1]) for p in parts]
    k = 0  # skip folders every part shares (the file's own top assembly)
    while paths and all(len(pp) > k for pp in paths) and len({pp[k] for pp in paths}) == 1:
        k += 1
    top: dict[str, list] = {}
    for p, pp in zip(parts, paths):
        if len(pp) > k:
            top.setdefault(pp[k], []).append((p.id, pp[k:]))
    names = {_norm(n): n for n in top if not _HW.search(n)}
    groups: dict[str, list[str]] = {}
    for name, items in top.items():
        if not _HW.search(name):
            groups.setdefault(name, []).extend(pid for pid, _ in items)
            continue
        for pid, pp in items:
            sub = _norm(_HW.sub("", pp[1])) if len(pp) > 1 else ""
            match = max((n for n in names if sub and sub.startswith(n)), key=len, default=None)
            if match:
                groups.setdefault(names[match], []).append(pid)
    return {n: g for n, g in groups.items() if len(g) >= 2}


def _flat(geos, ids, root, pinned, view, spread, aspect, ref, biases=(), asm=None, walls=(), bias0=0.0, box=None, axis=None, stable=False):
    """Explode `ids` around `root`. Bundles of rods move as one.

    Returns (offsets, lines, bundles {unit id: members}, assembled placement).
    """
    geos = dict(geos)
    pinned = dict(pinned)
    unit_of, inner_all, members_of = {}, {}, {}
    for members in _bundles(geos, ids):
        if root in members:
            continue
        u, inner = _bundle_unit(geos, members)
        pins = [m for m in members if m in pinned]
        if pins:
            bases = [pinned[m] - inner[m] for m in pins]
            if any(np.linalg.norm(b - bases[0]) > 0.05 for b in bases):
                continue  # pulled apart by hand: leave them as single parts
            for m in members:
                pinned.pop(m, None)
            pinned[u.id] = bases[0]
        geos[u.id] = u
        members_of[u.id] = members
        inner_all.update(inner)
        for m in members:
            unit_of[m] = u.id
    ids = [pid for pid in ids if pid not in unit_of] + list(members_of)
    parent = _parents(geos, ids, root)

    guides = None
    if stable:
        canonical = _run(geos, ids, root, parent, {}, view, 1.0, aspect, 0.0, ref=ref, axis=axis)
        guides = {p: _unit(o - canonical[0].get(parent.get(p), np.zeros(3)))
                  for p, o in canonical[0].items() if p != root
                  and np.linalg.norm(o - canonical[0].get(parent.get(p), np.zeros(3))) > 1e-6}
    best = _run(geos, ids, root, parent, pinned, view, spread, aspect, bias0, asm, ref, walls, box, axis, guides)
    if biases:
        bw, bh = best[3]
        need = 1 if bw / max(bh, 1e-6) > aspect else -1  # 1: needs height, -1: needs width
        for b in biases:
            res = _run(geos, ids, root, parent, pinned, view, spread, aspect, b * need, asm, ref, walls, box, axis)
            if res[2] > best[2] * 1.02:
                best = res
    offsets, lines, fit, _, asm_out = best
    ok = fit >= 0
    for uid, members in members_of.items():
        if uid in offsets:
            o = offsets.pop(uid)
            for m in members:
                offsets[m] = o + inner_all[m]
        if uid in lines:
            lines[members[0]] = lines.pop(uid)
    return offsets, lines, members_of, asm_out, ok


def projected_size(model, view, ids):
    """Width and height of the whole assembly, put together, in drawing units."""
    base = model_geo(model)
    _, M = _proj(view)
    Ha = np.max([_support(base[pid].verts @ M) for pid in ids if pid in base], axis=0)
    return float(Ha[0] + Ha[K // 2]), float(Ha[K // 4] + Ha[3 * K // 4])


def _rect_support(x0, y0, x1, y1):
    return _support(np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], float))


def _joint_layout(geos, ids, root, pins, view, spread, ref, frame, axis):
    """Optimize all group positions together in the available sheet space."""
    from .packing import pack
    _, M = _proj(view)
    ids = [root] + [p for p in ids if p != root]
    centers = np.array([geos[p].center @ M for p in ids])
    outlines = [_outlines(geos[p], M) - NORMALS @ centers[i] for i,p in enumerate(ids)]
    seeds = []
    for bias in (0.0, .6, -.6):
        canonical = _flat(geos, ids, root, pins, view, 1.0, frame["area"][2]/frame["area"][3], ref, bias0=bias, axis=axis)[0]
        seeds.append(np.array([centers[i] + np.asarray(canonical[p]) @ M for i,p in enumerate(ids)]))
    # Prefer a nearby larger neighbor only when it is substantially closer
    # than the main body. Volume ordering prevents cycles in the graph.
    parents = [0] * len(ids)
    volume = np.array([np.prod(geos[p].ext) for p in ids])
    for i,p in enumerate(ids[1:],1):
        root_distance = np.linalg.norm(geos[p].center-geos[root].center)
        candidates = [(np.linalg.norm(geos[p].center-geos[q].center),j)
                      for j,q in enumerate(ids) if j != i and volume[j] > volume[i]*1.1]
        if candidates:
            distance,parent = min(candidates)
            if distance < root_distance*.55:
                parents[i] = parent
    if os.environ.get("CADRAWS_DEBUG"):
        print("[joint] relationships",[(p,ids[parents[i]]) for i,p in enumerate(ids)],flush=True)
    cones = np.tile([0.,0.,-1.],(len(ids),1))
    for i in range(1,len(ids)):
        relative = centers[i]-centers[0]
        if parents[i] == 0 and np.linalg.norm(relative) > .6*ref:
            # A distant end group should not cross vertically through the
            # main body when contacts push it around. This is a broad half
            # plane, not a prescribed position or explosion distance.
            if abs(relative[1]) > np.linalg.norm(relative)*.25:
                cones[i] = [0.,float(np.sign(relative[1])),0.]
            else:
                cones[i] = [*_unit(relative),0.]
    rails = np.zeros((len(ids),2))
    for i,p in enumerate(ids):
        projected = geos[p].axes[0]@M
        if not p.startswith("group:") and geos[p].kind == "long" and np.linalg.norm(projected) > .3:
            rails[i] = _unit(projected)
            cones[i] = [0.,0.,-1.]
    key = (tuple(ids),tuple(np.round(M.ravel(),5)))
    signature = hashlib.sha1(
        np.concatenate(outlines).tobytes() + np.asarray(seeds[0]).tobytes()
        + json.dumps({k:v for k,v in frame.items() if k not in ("seed","seedPlace","seedView")},sort_keys=True).encode()
    ).digest()
    regions = [frame.get("regions",{}).get(getattr(geos[p],"main",p),frame.get("regions",{}).get(p)) for p in ids]
    previous = _joint_cache.get(key)
    if previous is not None and previous[3] == signature:
        positions,scale = previous[:2]
    else:
        summary = np.array([[h[:,k].max() for k in (0,16,32,48)] for h in outlines])
        small_edit = (previous is not None and abs(previous[4]-spread) < .3
                      and previous[5].shape == summary.shape
                      and np.max(np.abs(previous[5]-summary)) < .05*ref)
        warm = previous[:3] if small_edit else None
        region_edits = [False]*len(ids)
        if previous is not None and len(previous)>6:
            region_edits = [a!=b for a,b in zip(regions,previous[6])]
        seed = frame.get("seed")
        seed_place = frame.get("seedPlace")
        seed_view = frame.get("seedView")
        if (warm is None and seed and seed_place and seed_view
                and all(np.allclose(seed_view[k],view[k],atol=1e-5) for k in ("dir","xdir"))
                and all(getattr(geos[p],"main",p) in seed for p in ids)):
            k0 = float(seed_place["k"])
            positions0 = np.array(seed_place["c"]) + k0 * np.array([
                centers[i] + np.asarray(seed[getattr(geos[p],"main",p)])@M - seed_place["rc"]
                for i,p in enumerate(ids)])
            warm = (positions0,k0,np.asarray(frame["area"],float))
            region_edits = [r is not None for r in regions]
        positions, scale = pack(outlines,seeds,frame["area"],frame.get("obstacles",[]),spread,
                                [p == root or p in pins for p in ids],parents,cones,
                                warm=warm,clearance=frame.get("clearance",0.),attachments=centers-centers[0],rails=rails,max_travel=1.15*ref,regions=regions,region_edits=region_edits)
        if len(_joint_cache) >= 32:
            _joint_cache.pop(next(iter(_joint_cache)))
        _joint_cache[key] = (positions.copy(),scale,np.asarray(frame["area"],float),signature,spread,summary,regions)
    c = positions[0]
    offsets = {p: (np.asarray(pins[p]) if p in pins else M @ ((positions[i]-c)/scale - (centers[i]-centers[0])))
               for i,p in enumerate(ids)}
    offsets[root] = np.zeros(3)
    lines = {p: line for p,o in offsets.items() if p != root
             and (line := _line(geos[p],np.zeros(3),o)) is not None}
    return offsets,lines,{"k":float(scale),"c":c.tolist(),"rc":centers[0].tolist()}


def _line(g, base, off):
    mv = off - base
    n = float(np.linalg.norm(mv))
    if n < 1e-6:
        return None
    dvec = mv / n
    back = float(np.max((g.verts - g.center) @ (-dvec)))
    start, end = g.center + base, g.center + off - dvec * back
    return (start, end) if (end - start) @ dvec > 0.5 else None


def layout(model, view, visible, pinned=None, spread=1.0, aspect=2.0, assembled=None, frame=None, group_spread=None, nudge=None):
    """Returns (offsets {pid: [x,y,z]}, lines {pid: (start, end)}, root id, extra).

    Sub-assemblies from the STEP file are exploded on their own first, then
    placed around the main assembly as one block each.

    assembled: None, or {"scale": s, "pin": [x, y] or None} to add the whole
    assembly, put together, to the drawing. extra holds where it went
    ("assembled"), rods that move as one ("bundles") and sub-assemblies that
    move as one ("groups", keyed by their main part).
    """
    base = model_geo(model)
    ids = [pid for pid in visible if pid in base]
    if not ids:
        return {}, {}, None, {}
    pinned = {k: np.array(v, float) for k, v in (pinned or {}).items() if k in base}
    root = max(ids, key=lambda p: base[p].vol)
    _, M = _proj(view)
    ref = float(np.hypot(*np.ptp(base[root].verts @ M, axis=0))) or 1.0

    # the assembly's long axis and its middle (the main part): parts keep their front-to-back order
    lo = np.min([base[p].bbox[:3] for p in ids], axis=0)
    hi = np.max([base[p].bbox[3:] for p in ids], axis=0)
    a3 = WORLD[int(np.argmax(hi - lo))]
    axis = (a3, float(base[root].center @ a3))

    groups = step_groups(model, ids)
    group_of = {pid: name for name, g in groups.items() for pid in g}
    root_group = group_of.get(root)
    if root_group is None:  # the main part stands alone: it is its own group
        root_group = "\0root"
        groups[root_group] = [root]
        group_of[root] = root_group

    def build(lean):
        """Whole layout with this lean (> 0 stacks parts up/down, < 0 spreads them sideways)."""
        geos = dict(base)
        outer_ids = [pid for pid in ids if pid not in group_of]
        outer_pins = {k: v for k, v in pinned.items() if k not in group_of}
        inner, mains, late, bundles = {}, {}, {}, {}
        rng = np.random.default_rng(0)
        for name, members in groups.items():
            is_root = name == root_group
            main = root if is_root else max(members, key=lambda p: base[p].vol)
            U = np.zeros(3) if is_root else pinned.get(main)
            local_pins = {}
            for m in members:
                if m in pinned and m != main:
                    if U is not None:
                        local_pins[m] = pinned[m] - U
                    else:
                        # A single hand-moved member is a separate fixed
                        # obstacle, not an auto-placed copy of itself inside
                        # the group footprint.
                        late[m] = pinned[m]
                        outer_ids.append(m)
                        outer_pins[m] = pinned[m]
            active = [m for m in members if m not in late]
            gs = float((group_spread or {}).get(main, 1.0))  # this sub-assembly's own spacing
            local_ref = math.sqrt(ref * min(ref, float(np.hypot(*np.ptp(base[main].verts @ M, axis=0))) or ref))
            loff, llines, lb, _, _ = _flat(base, active, main, local_pins, view, spread * gs, aspect, local_ref, bias0=lean, axis=axis, stable=True)
            bundles.update(lb)
            inner[name] = (main, loff, llines)
            mains[name] = main
            # the exploded group becomes one block for the outer layout
            u = Geo()
            u.id = "group:" + name
            u.main = main
            verts = np.concatenate([base[m].verts + loff.get(m, 0) for m in active])
            if len(verts) > 8000:
                verts = verts[rng.choice(len(verts), 8000, replace=False)]
            u.verts = verts
            u.parts = [base[m].verts + loff.get(m, 0) for m in active]
            boxes = np.array([base[m].bbox for m in active])
            u.bbox = np.concatenate([boxes[:, :3].min(axis=0), boxes[:, 3:].max(axis=0)])
            u.center = base[main].center
            u.vol = float(np.prod(np.maximum(u.bbox[3:] - u.bbox[:3], 1e-3)))
            g0 = base[main]
            u.axes, u.ext, u.kind = g0.axes, g0.ext, g0.kind
            geos[u.id] = u
            outer_ids.append(u.id)
            if U is not None and not is_root:
                outer_pins[u.id] = U

        asm = None
        if assembled:
            Ha = np.max([_support(base[pid].verts @ M) for pid in ids], axis=0)
            pin = assembled.get("pin")
            asm = (Ha, float(assembled.get("scale", 0.5)), pin if pin and len(pin) == 2 else None)

        root_unit = "group:" + root_group
        biases = (0.15, 0.3, 0.5, 0.8)
        place = None
        if framed:
            if frame.get("k") and frame.get("anchor"):
                ooff, olines, ob, asm_out, _ = _flat(geos, outer_ids, root_unit, outer_pins, view, spread, aspect, ref, axis=axis)
                place = {"k": float(frame["k"]), "c": frame["anchor"],
                         "rc": (geos[root_unit].center @ M).tolist()}
            else:
                ooff, olines, place = _joint_layout(geos, outer_ids, root_unit, outer_pins, view, spread, ref, frame, axis)
                ob, asm_out = {}, None
        else:
            ooff, olines, ob, asm_out, _ = _flat(geos, outer_ids, root_unit, outer_pins, view, spread, aspect, ref, biases, asm, (), lean, None, axis)
        bundles.update(ob)

        offsets = {pid: o for pid, o in ooff.items() if not pid.startswith("group:")}
        lines = {pid: ln for pid, ln in olines.items() if not pid.startswith("group:")}
        for name, (main, loff, llines) in inner.items():
            U = ooff.get("group:" + name, np.zeros(3))
            for m, o in loff.items():
                offsets[m] = U + o
            for m, (a, b) in llines.items():
                lines[m] = (a + U, b + U)
            if "group:" + name in olines:
                lines[main] = olines["group:" + name]
        for m, pos in late.items():
            offsets[m] = pos
            ln = _line(base[m], offsets.get(mains[group_of[m]], np.zeros(3)), pos)
            if ln:
                lines[m] = ln
            else:
                lines.pop(m, None)
        return offsets, lines, bundles, place, asm_out, mains

    # try a few leans and keep the one that comes out biggest in the space there is
    framed = bool(frame and frame.get("area"))
    settled = frame and frame.get("k") or all(p in pinned for p in ids if p != root)
    leans = (0.0,) if settled or framed else (0.0, 0.3, 0.6, 1.0, -0.3, -0.6)
    best = None
    for lean in leans:
        r = build(lean)
        if framed:
            score = r[3]["k"]
        else:
            lo, hi = np.array([np.inf, np.inf]), np.array([-np.inf, -np.inf])
            for pid, o in r[0].items():
                p2 = (base[pid].verts + o) @ M
                lo, hi = np.minimum(lo, p2.min(axis=0)), np.maximum(hi, p2.max(axis=0))
            bw, bh = hi - lo
            score = min(aspect / max(bw, 1e-6), 1.0 / max(bh, 1e-6))
        if os.environ.get("CADRAWS_DEBUG"):
            print(f"[lean] {lean:+.2f} score={score:.5f}", flush=True)
        if best is None or score > best[0] * 1.005:
            best = (score, r)
    offsets, lines, bundles, place, asm_out, mains = best[1]

    # parts just dropped by hand: slide them the shortest way off anything they overlap
    moved = [m for m in (nudge or []) if m in offsets]
    if moved:
        sup = {pid: _support((base[pid].verts + offsets[pid]) @ M) for pid in offsets if pid in base}
        Hm = np.max([sup[m] for m in moved], axis=0)
        others = [h for pid, h in sup.items() if pid not in moved]
        gap = GAP * ref * spread
        best_s, best_u = None, None
        for a in np.linspace(0, 2 * math.pi, 32, endpoint=False):
            u = np.array([math.cos(a), math.sin(a)])
            s_ = _free_distance(Hm, u, others, gap)
            if best_s is None or s_ < best_s:
                best_s, best_u = s_, u
        if best_s and best_s > 1e-6:
            v3 = M @ (best_u * best_s)  # back to 3D, in the view plane
            for m in moved:
                offsets[m] = offsets[m] + v3
                if m in lines:
                    a0, b0 = lines[m]
                    lines[m] = (a0, b0 + v3)

    extra = {
        "bundles": {m: members for members in bundles.values() for m in members},
        "groups": {mains[n]: g for n, g in groups.items() if n != root_group and len(g) > 1},
        "group_of": {m: mains[n] for n, g in groups.items() if len(g) > 1 for m in g},
    }
    if place:
        extra["place"] = place
    if asm_out:
        extra["assembled"] = {k: [round(float(x), 3) for x in v] if k != "scale" else v for k, v in asm_out.items()}
    return (
        {pid: [round(float(v), 4) for v in o] for pid, o in offsets.items()},
        {pid: ([float(v) for v in a], [float(v) for v in b]) for pid, (a, b) in lines.items()},
        root,
        extra,
    )


def hull2d(pts):
    """Convex hull (monotone chain) of 2D points, as a list of [x, y]."""
    P = np.unique(np.round(pts, 3), axis=0)
    if len(P) < 3:
        return P.tolist()
    P = P[np.lexsort((P[:, 1], P[:, 0]))]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(tuple(p))
    for p in P[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(tuple(p))
    return [list(map(float, p)) for p in lower[:-1] + upper[:-1]]


def part_outlines(model, view, offsets, ids):
    """Convex outline of each moved part in drawing coords (for clicking)."""
    geos = model_geo(model)
    _, M = _proj(view)
    out = {}
    for pid in ids:
        g = geos.get(pid)
        if g is None:
            continue
        o = np.array(offsets.get(pid, [0, 0, 0]), float)
        pts = (g.verts + o) @ M
        if len(pts) > 1500:
            pts = pts[:: max(1, len(pts) // 1500)]
        out[pid] = [[round(x, 2), round(y, 2)] for x, y in hull2d(pts)]
    return out


def segment_hits(model, view, offsets, ids, a, b):
    """Parts whose outline the 2D segment a-b passes over."""
    geos = model_geo(model)
    _, M = _proj(view)
    a2, b2 = np.array(a) @ M, np.array(b) @ M
    ts = np.linspace(0, 1, 24)
    samples = a2[None, :] + (b2 - a2)[None, :] * ts[:, None]
    hits = []
    for pid in ids:
        g = geos.get(pid)
        if g is None:
            continue
        o = np.array(offsets.get(pid, [0, 0, 0]), float)
        H = _support((g.verts + o) @ M)
        inside = (samples @ NORMALS.T <= H[None, :] + 1e-6).all(axis=1)
        if inside.any():
            hits.append(pid)
    return hits


# ----------------------------------------------------------------------------
# Fixed views
# ----------------------------------------------------------------------------
def preset_view(model, az=30.0, el=18.0, flip=False):
    """View from the view cube angles.

    Cube frame from the biggest part: FRONT looks at its thinnest side, TOP
    is its middle-sized axis, RIGHT completes the frame. az turns from
    FRONT toward RIGHT, el tilts toward TOP (degrees). `flip` swaps top and
    bottom.
    """
    geos = model_geo(model)
    root = max(geos.values(), key=lambda g: g.vol)
    ext = root.bbox[3:] - root.bbox[:3]
    order = np.argsort(ext)  # thin, mid, long
    F = WORLD[order[0]].copy()
    T = WORLD[order[1]] * (-1.0 if flip else 1.0)
    R = np.cross(T, F)
    a, e = math.radians(az), math.radians(el)
    d = math.cos(e) * (math.cos(a) * F + math.sin(a) * R) + math.sin(e) * T
    x = -math.sin(a) * F + math.cos(a) * R
    return {"dir": [float(v) for v in _unit(d)], "xdir": [float(v) for v in _unit(x)]}


def supports(model, view, offsets, ids):
    geos = model_geo(model)
    _, M = _proj(view)
    out = {}
    for pid in ids:
        g = geos.get(pid)
        if g is None:
            continue
        o = np.array(offsets.get(pid, [0, 0, 0]), float)
        out[pid] = _support((g.verts + o) @ M)
    return out


def segment_hits_fast(view, sup, a, b, skip=()):
    """Parts (by precomputed outline) that the 2D segment a-b passes over."""
    _, M = _proj(view)
    a2, b2 = np.array(a) @ M, np.array(b) @ M
    ts = np.linspace(0, 1, 24)
    samples = a2[None, :] + (b2 - a2)[None, :] * ts[:, None]
    proj = samples @ NORMALS.T  # (24, K)
    return [pid for pid, H in sup.items() if pid not in skip and (proj <= H[None, :] + 1e-6).all(axis=1).any()]
