"""Keeps loaded models in memory and caches them on disk.

Reading a big STEP file can take a minute. After the first load the parts
are written as native OpenCascade .brep files plus a mesh blob, which load
in a few seconds.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS_Shape

from .mesher import pack_meshes
from .step_loader import Part, load_step

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".cache")
CACHE_VERSION = 3


@dataclass
class Model:
    id: str
    source: str
    name: str
    parts: list[Part]
    mesh: bytes

    def part(self, pid: str) -> Part:
        for p in self.parts:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def info(self):
        bb = _union_bbox(self.parts)
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "bbox": bb,
            "parts": [
                {
                    "id": p.id,
                    "name": p.name,
                    "path": p.path,
                    "color": p.color,
                    "center": p.center,
                    "bbox": p.bbox,
                }
                for p in self.parts
            ],
        }


def _union_bbox(parts):
    if not parts:
        return [0, 0, 0, 1, 1, 1]
    b = list(parts[0].bbox)
    for p in parts[1:]:
        for i in range(3):
            b[i] = min(b[i], p.bbox[i])
            b[i + 3] = max(b[i + 3], p.bbox[i + 3])
    return b


def model_id_for(path: str) -> str:
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|v{CACHE_VERSION}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class Store:
    def __init__(self):
        self.models: dict[str, Model] = {}
        self.lock = threading.Lock()
        self.status: dict[str, str] = {}

    def open(self, path: str) -> Model:
        path = os.path.abspath(os.path.expanduser(path))
        mid = model_id_for(path)
        with self.lock:
            if mid in self.models:
                return self.models[mid]
            model = self._load_cached(mid, path) or self._load_step(mid, path)
            self.models[mid] = model
            return model

    def get(self, mid: str) -> Model:
        return self.models[mid]

    # ------------------------------------------------------------------
    def cache_path(self, mid):
        return os.path.join(CACHE_DIR, mid)

    _cache_path = cache_path

    def _load_step(self, mid: str, path: str) -> Model:
        t = time.time()
        self.status[mid] = "Reading STEP file"
        asm = load_step(path)
        self.status[mid] = f"Meshing {len(asm.parts)} parts"
        mesh = pack_meshes(asm.parts)
        model = Model(mid, path, os.path.splitext(os.path.basename(path))[0], asm.parts, mesh)
        self._write_cache(model)
        self.status[mid] = f"Loaded in {time.time() - t:.1f}s"
        return model

    def _write_cache(self, model: Model):
        d = self._cache_path(model.id)
        os.makedirs(os.path.join(d, "shapes"), exist_ok=True)
        for p in model.parts:
            BRepTools.Write_s(p.shape, os.path.join(d, "shapes", f"{p.id}.brep"))
        with open(os.path.join(d, "mesh.bin"), "wb") as f:
            f.write(model.mesh)
        meta = {
            "version": CACHE_VERSION,
            "source": model.source,
            "name": model.name,
            "parts": [
                {"id": p.id, "name": p.name, "path": p.path, "color": p.color, "bbox": p.bbox}
                for p in model.parts
            ],
        }
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f)
        self._drop_stale(model)

    def _drop_stale(self, model: Model):
        """Remove caches of older versions of the same file."""
        import shutil

        for name in os.listdir(CACHE_DIR):
            if name == model.id:
                continue
            meta = os.path.join(CACHE_DIR, name, "meta.json")
            try:
                with open(meta) as f:
                    src = json.load(f).get("source")
            except Exception:  # noqa: BLE001
                src = None
            if src == model.source or not os.path.isfile(meta):
                shutil.rmtree(os.path.join(CACHE_DIR, name), ignore_errors=True)
                self.models.pop(name, None)

    def _load_cached(self, mid: str, path: str) -> Model | None:
        d = self._cache_path(mid)
        meta_path = os.path.join(d, "meta.json")
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get("version") != CACHE_VERSION:
                return None
            parts = []
            builder = BRep_Builder()
            for pm in meta["parts"]:
                shape = TopoDS_Shape()
                ok = BRepTools.Read_s(shape, os.path.join(d, "shapes", f"{pm['id']}.brep"), builder)
                if not ok:
                    return None
                parts.append(
                    Part(
                        id=pm["id"],
                        name=pm["name"],
                        path=pm["path"],
                        shape=shape,
                        color=tuple(pm["color"]) if pm["color"] else None,
                        bbox=tuple(pm["bbox"]),
                    )
                )
            with open(os.path.join(d, "mesh.bin"), "rb") as f:
                mesh = f.read()
            return Model(mid, path, meta["name"], parts, mesh)
        except Exception:
            return None


STORE = Store()
