"""Local web server."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import traceback

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import fonts as fontlib
from .hlr import View
from .render import render as render_parallel
from .layout import layout as auto_layout
from .layout import part_outlines, preset_view, projected_size, segment_hits_fast, supports
from .render import probe_model
from .render import shutdown as shutdown_pool
from .store import ROOT, STORE

WEB = os.path.join(ROOT, "web")
PROJECTS = os.path.join(ROOT, "projects")
UPLOADS = os.path.join(ROOT, "uploads")
RECENT = os.path.join(PROJECTS, ".recent.json")
os.makedirs(PROJECTS, exist_ok=True)
os.makedirs(UPLOADS, exist_ok=True)

app = FastAPI()


@app.middleware("http")
async def _no_stale_files(request, call_next):
    # the browser must check for a newer copy of the page and its scripts every time,
    # or an update leaves new buttons wired to old code
    res = await call_next(request)
    if not request.url.path.startswith("/api/fontfile"):
        res.headers["Cache-Control"] = "no-cache"
    return res
_render_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class OpenReq(BaseModel):
    path: str


def _remember(path: str):
    try:
        recent = json.load(open(RECENT)) if os.path.isfile(RECENT) else []
    except Exception:
        recent = []
    recent = [p for p in recent if p != path]
    recent.insert(0, path)
    json.dump(recent[:15], open(RECENT, "w"))


def _open(path: str):
    path = os.path.abspath(os.path.expanduser(path.strip().strip("'\"")))
    if not os.path.isfile(path):
        raise HTTPException(404, f"File not found: {path}")
    if not re.search(r"\.(step|stp)$", path, re.I):
        raise HTTPException(400, "Pick a .step or .stp file")
    try:
        model = STORE.open(path)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(500, f"Could not load model: {e}") from e
    _remember(path)
    probe_model(model, STORE.cache_path(model.id))
    info = model.info()
    sp = _state_path(path)
    if os.path.isfile(sp):
        try:
            with open(sp) as f:
                info["saved"] = json.load(f).get("state")
        except Exception:  # noqa: BLE001
            pass
    return info


@app.post("/api/open")
def api_open(req: OpenReq):
    return _open(req.path)


@app.post("/api/upload")
def api_upload(file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "upload.step")
    if not re.search(r"\.(step|stp)$", name, re.I):
        raise HTTPException(400, "Pick a .step or .stp file")
    dest = os.path.join(UPLOADS, name)
    with open(dest, "wb") as f:
        while chunk := file.file.read(1 << 20):
            f.write(chunk)
    return _open(dest)


@app.get("/api/recent")
def api_recent():
    try:
        recent = json.load(open(RECENT)) if os.path.isfile(RECENT) else []
    except Exception:
        recent = []
    return [p for p in recent if os.path.isfile(p)]


def _model(mid: str):
    try:
        return STORE.get(mid)
    except KeyError:
        raise HTTPException(404, "Model not loaded. Open the STEP file again.") from None


@app.get("/api/model/{mid}/mesh")
def api_mesh(mid: str):
    return Response(_model(mid).mesh, media_type="application/octet-stream")


# ----------------------------------------------------------------------------
# Draw: lay out the exploded view and make the line drawing in one call
# ----------------------------------------------------------------------------
class DrawReq(BaseModel):
    model: str
    view: dict = {"az": 30, "el": 18, "flip": False}
    spread: float = 1.0
    aspect: float = 2.0
    hidden: list[str] = []
    pinned: dict[str, list[float]] = {}
    nudge: list[str] = []  # parts just dropped by hand: move them off anything they overlap
    group_spread: dict[str, float] = {}  # sub-assembly main part id -> its own spread factor
    engine: str = "fast"
    lines: bool = True
    assembled: dict | None = None  # any value: also draw the whole thing, put together, on its own
    frame: dict | None = None  # {"area", "obstacles", "asm": {corner, w, inset, pad}?, "anchor"?}, sheet mm


def _path_d(polys):
    out = []
    for poly in polys:
        x, y = poly[0]
        seg = [f"M{x:.2f} {y:.2f}"]
        for x, y in poly[1:]:
            seg.append(f"L{x:.2f} {y:.2f}")
        out.append("".join(seg))
    return "".join(out)


def _anchor(res):
    """A point on the part's own visible lines, close to its middle."""
    pts = [p for kind in ("sharp", "outline") for poly in res.get(kind, []) for p in poly]
    if not pts:
        return None, None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bbox = [min(xs), min(ys), max(xs), max(ys)]
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    best = min(pts, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
    return [round(best[0], 2), round(best[1], 2)], [round(v, 2) for v in bbox]


@app.post("/api/draw")
def api_draw(req: DrawReq):
    model = _model(req.model)
    hidden = set(req.hidden)
    visible = [p.id for p in model.parts if p.id not in hidden]
    if not visible:
        raise HTTPException(400, "Every part is hidden")
    v = req.view or {}
    view = preset_view(model, float(v.get("az", 30)), float(v.get("el", 18)), bool(v.get("flip")))
    t = time.time()
    # the assembled view sits in a corner of the parts area at the width asked for;
    # its height follows the assembly's own shape. The parts go around it.
    frame, asm_box = req.frame, None
    if frame and frame.get("asm"):
        a = frame["asm"]
        ax, ay, aw, ah = frame["area"]
        bw, bh = projected_size(model, view, visible)
        inset = float(a.get("inset", 0))
        corner = str(a.get("corner", "br"))
        w = min(float(a.get("w", aw * 0.4)), aw)
        h = w * bh / max(bw, 1e-6)
        hmax = max(min(ah * 0.7, ah - inset), 1.0)  # stays inside the column, below/above same-corner logos
        if h > hmax:
            h = hmax
            w = h * bw / max(bh, 1e-6)

        def at(w, h):
            return (ax + aw - w if corner.endswith("r") else ax), (ay + inset if corner.startswith("t") else ay + ah - inset - h)

        x, y = at(w, h)
        # logos in another corner: shrink (from its own corner) until clear of them
        for _ in range(60):
            if not any(x < ox + ow and ox < x + w and y < oy + oh and oy < y + h for ox, oy, ow, oh in frame.get("obstacles") or []):
                break
            w, h = w * 0.95, h * 0.95
            x, y = at(w, h)
        asm_box = [round(x, 2), round(y, 2), round(w, 2), round(h, 2)]
        pad = float(a.get("pad", 0))
        frame = {**frame, "obstacles": list(frame.get("obstacles") or []) + [[x - pad, y - pad, w + 2 * pad, h + 2 * pad]]}
    with _render_lock:
        try:
            offsets, lines, _root, extra = auto_layout(model, view, visible, req.pinned, req.spread, req.aspect, None, frame, req.group_spread, req.nudge)
            transforms = {
                pid: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, o[0], o[1], o[2], 1]
                for pid, o in offsets.items()
                if any(abs(c) > 1e-9 for c in o)
            }
            batches = []
            sup = supports(model, view, offsets, visible)
            if req.lines and lines and req.engine != "preview":
                items = sorted(lines.items())
                for i in range(0, len(items), 8):
                    chunk = items[i : i + 8]
                    occ = set()
                    for pid, (a, b) in chunk:
                        occ.update(segment_hits_fast(view, sup, a, b, skip={pid}))
                    batches.append((sorted(occ), [(pid, a, b) for pid, (a, b) in chunk]))
            result = render_parallel(
                model,
                STORE.cache_path(model.id),
                transforms,
                visible,
                View(dir=tuple(view["dir"]), xdir=tuple(view["xdir"])),
                engine=req.engine,
                line_batches=batches,
                outlines=sup,
                assembled=req.assembled is not None,
            )
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            raise HTTPException(500, f"Drawing failed: {e}") from e
    if req.engine == "preview" and req.lines:
        # Interactive previews reuse isolated part contours and simple guide
        # segments. Final exports calculate mutual occlusion and hidden guides.
        from .layout import _proj
        import numpy as np
        _, projection = _proj(view)
        result["lines"] = {pid: [(np.asarray(a) @ projection).tolist(),
                                  (np.asarray(b) @ projection).tolist()]
                           for pid, (a, b) in lines.items()}
        result["lines"] = {pid: [segment] for pid, segment in result["lines"].items()}
    outlines = part_outlines(model, view, offsets, visible)
    parts = {}
    for pid, res in result["parts"].items():
        anchor, bbox = _anchor(res)
        parts[pid] = {
            "paths": {k: _path_d(vv) for k, vv in res.items() if vv},
            "anchor": anchor,
            "bbox": bbox,
            "outline": outlines.get(pid),
        }
    bounds = list(result["bounds"])
    assembled = None
    if req.assembled is not None and result["assembled"]:
        polys = [poly for pid in visible for kind in ("sharp", "outline") for poly in result["assembled"].get(pid, {}).get(kind, [])]
        pts = [p for poly in polys for p in poly]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            assembled = {"d": _path_d(polys), "bbox": [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]}
    return {
        "view": view,
        "offsets": offsets,
        "parts": parts,
        "lines": {pid: _path_d(polys) for pid, polys in result["lines"].items() if polys},
        "bundles": extra.get("bundles", {}),
        "groups": extra.get("groups", {}),
        "groupOf": extra.get("group_of", {}),
        "place": extra.get("place"),
        "asmBox": asm_box,
        "assembled": assembled,
        "bounds": [round(x, 2) for x in bounds],
        "engine": req.engine,
        "seconds": round(time.time() - t, 1),
    }


@app.on_event("shutdown")
def _shutdown():
    shutdown_pool()


# ----------------------------------------------------------------------------
# Saved settings, one file per STEP file (kept automatically)
# ----------------------------------------------------------------------------
def _state_path(source: str) -> str:
    import hashlib

    return os.path.join(PROJECTS, hashlib.sha1(os.path.abspath(source).encode()).hexdigest()[:16] + ".json")


class StateReq(BaseModel):
    source: str
    state: dict


@app.put("/api/state")
def api_state_put(req: StateReq):
    with open(_state_path(req.source), "w") as f:
        json.dump({"source": req.source, "state": req.state}, f)
    return {"ok": True}


@app.get("/api/version")
def api_version():
    return {"version": 2}


@app.get("/api/fonts")
def api_fonts():
    return [{k: f[k] for k in ("id", "family", "style", "full", "bold")} for f in fontlib.fonts()]


@app.get("/api/fontfile/{fid}")
def api_fontfile(fid: str):
    try:
        data = fontlib.font_bytes(fid)
    except KeyError:
        raise HTTPException(404, "No such font") from None
    return Response(data, media_type="font/ttf", headers={"Cache-Control": "max-age=86400"})


fontlib.warm()


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"), headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=WEB), name="web")


@app.exception_handler(Exception)
async def _err(_req, exc):
    return JSONResponse({"detail": str(exc)}, status_code=500)
