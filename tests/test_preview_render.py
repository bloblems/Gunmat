import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from cadraw import render as renderer
from cadraw.hlr import View


class PreviewCacheTests(unittest.TestCase):
    def test_translated_contours_are_reused_without_mutating_cache(self):
        calls = []

        class FakePool:
            def schedule(self, function, args, timeout):
                calls.append(args)
                parts = {
                    pid: {"sharp": [[[0.0, 0.0], [2.0, 1.0]]]} for pid, _ in args[1]
                }
                future = Future()
                future.set_result({"parts": parts, "timing": {}})
                return future

        model = SimpleNamespace(parts=[SimpleNamespace(id="part", name="Part")])
        view = View(dir=(0.0, 0.0, 1.0), xdir=(1.0, 0.0, 0.0))
        renderer._results.clear()
        with (
            patch.object(renderer, "pool", return_value=FakePool()),
            patch.object(renderer, "heavy_parts", return_value=set()),
            patch.object(renderer.os.path, "getsize", return_value=1),
        ):
            baseline = renderer.render(
                model, "/fixture", {}, ["part"], view, engine="preview"
            )
            transform = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, 20, 0, 1]
            moved = renderer.render(
                model, "/fixture", {"part": transform}, ["part"], view, engine="preview"
            )
            again = renderer.render(
                model, "/fixture", {}, ["part"], view, engine="preview"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            moved["parts"]["part"]["sharp"], [[[10.0, -20.0], [12.0, -19.0]]]
        )
        self.assertEqual(baseline["parts"], again["parts"])
        self.assertEqual(moved["cached"], 1)
        renderer._results.clear()
