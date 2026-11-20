"""Installed fonts: list them and hand out single font faces.

Collections (.ttc) are split into single faces so the browser can load them
and turn text into outlines on export.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

from .store import CACHE_DIR

if sys.platform == "darwin":
    FONT_DIRS = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
elif os.name == "nt":
    FONT_DIRS = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Microsoft/Windows/Fonts"]
else:
    FONT_DIRS = [Path(p) / "fonts" for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p]
    FONT_DIRS += [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "fonts", Path.home() / ".fonts"]
EXTS = (".ttf", ".otf", ".ttc", ".otc")
INDEX = os.path.join(CACHE_DIR, "fonts.json")

_fonts: list[dict] | None = None
_by_id: dict[str, dict] = {}
_bytes: dict[str, bytes] = {}
_lock = threading.Lock()


def _name(font, *ids):
    tbl = font["name"]
    for i in ids:
        rec = tbl.getDebugName(i)
        if rec:
            return rec
    return ""


def _files():
    out = []
    for d in FONT_DIRS:
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() in EXTS and p.is_file():
                out.append((str(p), int(p.stat().st_mtime)))
    return out


def _scan():
    files = _files()
    sig = hashlib.sha1(json.dumps(files).encode()).hexdigest()
    if os.path.isfile(INDEX):
        try:
            with open(INDEX) as f:
                data = json.load(f)
            if data.get("sig") == sig:
                return data["fonts"]
        except Exception:  # noqa: BLE001
            pass
    fonts = []
    for path, _ in files:
        try:
            if path.lower().endswith((".ttc", ".otc")):
                coll = TTCollection(path, lazy=True)
                faces = list(enumerate(coll.fonts))
            else:
                faces = [(None, TTFont(path, lazy=True))]
            for idx, font in faces:
                family = _name(font, 16, 1)
                style = _name(font, 17, 2) or "Regular"
                if not family or family.startswith("."):
                    continue
                full = _name(font, 4) or f"{family} {style}"
                fid = hashlib.sha1(f"{path}#{idx}".encode()).hexdigest()[:10]
                fonts.append({"id": fid, "family": family, "style": style, "full": full, "path": path, "index": idx})
        except Exception:  # noqa: BLE001
            continue
    # bold sibling per family, for labels and part numbers
    fams: dict[str, list[dict]] = {}
    for f in fonts:
        fams.setdefault(f["family"], []).append(f)
    for faces in fams.values():
        bold = next((f for f in faces if f["style"].lower() == "bold"), None) or next(
            (f for f in faces if "bold" in f["style"].lower() and "italic" not in f["style"].lower() and "oblique" not in f["style"].lower()), None
        )
        for f in faces:
            f["bold"] = bold["id"] if bold else f["id"]
    fonts.sort(key=lambda f: (f["family"].lower(), f["style"].lower()))
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(INDEX, "w") as fh:
        json.dump({"sig": sig, "fonts": fonts}, fh)
    return fonts


def fonts() -> list[dict]:
    global _fonts
    with _lock:
        if _fonts is None:
            _fonts = _scan()
            _by_id.update({f["id"]: f for f in _fonts})
        return _fonts


def font_bytes(fid: str) -> bytes:
    fonts()
    if fid in _bytes:
        return _bytes[fid]
    f = _by_id[fid]
    if f["index"] is None:
        with open(f["path"], "rb") as fh:
            data = fh.read()
    else:
        coll = TTCollection(f["path"])
        buf = io.BytesIO()
        coll.fonts[f["index"]].save(buf)
        data = buf.getvalue()
    _bytes[fid] = data
    return data


def warm():
    threading.Thread(target=fonts, daemon=True).start()
