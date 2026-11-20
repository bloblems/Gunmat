import unittest

import numpy as np

from cadraw.layout import Geo, _joint_layout, _proj, _run


class FrameFitTests(unittest.TestCase):
    def test_multiple_parts_fit_without_overlap(self):
        view = {"dir": [0, 0, 1], "xdir": [1, 0, 0]}
        geos = {}
        for i in range(9):
            g = Geo()
            g.id = str(i)
            g.verts = np.array(
                [[x, y, z] for x in (-10, 10) for y in (-5, 5) for z in (-1, 1)], float
            )
            g.center = np.zeros(3)
            g.bbox = np.r_[g.verts.min(axis=0), g.verts.max(axis=0)]
            g.vol, g.ext, g.kind = 400, [20, 10, 2], "flat"
            g.axes = list(np.eye(3))
            geos[g.id] = g
        result = _run(
            geos,
            list(geos),
            "0",
            {str(i): "0" for i in range(1, 9)},
            {},
            view,
            1,
            2,
            0,
            ref=20,
            box=(-55, -30, 55, 30),
        )
        self.assertGreater(result[2], 0)
        _, matrix = _proj(view)
        rectangles = []
        for pid, offset in result[0].items():
            points = (geos[pid].verts + offset) @ matrix
            lo, hi = points.min(axis=0), points.max(axis=0)
            self.assertTrue(np.all(lo >= [-55, -30]))
            self.assertTrue(np.all(hi <= [55, 30]))
            for other_lo, other_hi in rectangles:
                self.assertTrue(np.any(lo >= other_hi) or np.any(other_lo >= hi))
            rectangles.append((lo, hi))

    def test_spread_increases_separation_without_overlap(self):
        from cadraw.layout import _rect_support, _spread_distance

        shape = _rect_support(-10, -5, 10, 5)
        direction = np.array([1.0, 0.0])
        compact = _spread_distance(shape, direction, [shape], 20, 1)
        expanded = _spread_distance(shape, direction, [shape], 20, 2)
        self.assertGreater(expanded, compact)
        self.assertLess(
            expanded - compact, 2
        )  # spread adds clearance, not multiples of component length

    def test_offset_center_fills_frame_without_clipping(self):
        view = {"dir": [0, 0, 1], "xdir": [1, 0, 0]}
        g = Geo()
        g.id = "root"
        g.verts = np.array(
            [[x, y, z] for x in (90, 110) for y in (15, 25) for z in (-1, 1)], float
        )
        g.center = g.verts.mean(axis=0)
        g.bbox = np.r_[g.verts.min(axis=0), g.verts.max(axis=0)]
        g.vol = 400
        g.axes = list(np.eye(3))
        g.ext = [20, 10, 2]
        g.kind = "flat"
        for origin in ((0, 0), (10, 25)):
            with self.subTest(origin=origin):
                frame = {"area": [*origin, 200, 100], "obstacles": []}
                *_, place = _joint_layout(
                    {"root": g}, ["root"], "root", {}, view, 1, 20, frame, None
                )
                _, matrix = _proj(view)
                points = np.array(place["c"]) + place["k"] * (
                    g.verts @ matrix - np.array(place["rc"])
                )
                self.assertTrue(np.all(points.min(axis=0) >= np.array(origin)))
                self.assertTrue(
                    np.all(points.max(axis=0) <= np.array(origin) + [200, 100])
                )
                self.assertGreater(
                    place["k"], 9.5
                )  # preserves the screen-space border clearance


if __name__ == "__main__":
    unittest.main()
