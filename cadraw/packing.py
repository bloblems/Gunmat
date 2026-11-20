"""Joint screen-space placement of compound part groups.

Geometry, boundaries, and reserved regions are hard constraints. Group
relationships are soft preferences, rather than prescribed coordinates.
"""

from __future__ import annotations

import math
import os

import numpy as np

# Sixteen separating axes bound each projected outline conservatively.
ANGLES = np.arange(16) * (2 * np.pi / 16)
N = np.c_[np.cos(ANGLES), np.sin(ANGLES)]
OPPOSITE = (np.arange(16) + 8) % 16


def _rect(rect):
    x, y, w, h = rect
    return np.max(
        np.array([[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) @ N.T, axis=0
    )


class Problem:
    def __init__(self, outlines, area, obstacles, gap, fixed, parents, cones, rails):
        self.counts = np.array([len(h) for h in outlines])
        self.owner = np.repeat(np.arange(len(outlines)), self.counts)
        self.local = np.concatenate(outlines)[:, ::4]
        self.area = np.asarray(area, float)
        self.obstacles = np.array(
            [_rect(r) for r in obstacles if r[2] > 0 and r[3] > 0]
        ).reshape(-1, 16)
        self.gap = gap
        self.fixed = np.asarray(fixed, bool)
        self.parents = np.asarray(parents, int)
        self.cones = np.asarray(cones, float)
        self.rails = np.asarray(rails, float)
        self.rail_origins = None
        self.travel_radius = np.full(len(outlines), math.inf)
        self.edit_origins = None
        self.edit_radius = np.full(len(outlines), 12.0)
        self.regions = np.tile(self.area, (len(outlines), 1))
        a, b = np.triu_indices(len(self.local), 1)
        keep = self.owner[a] != self.owner[b]
        self.a, self.b = a[keep], b[keep]
        self.ga, self.gb = self.owner[self.a], self.owner[self.b]
        x, y, w, h = self.area
        self.grid = np.array(
            [
                [xx, yy]
                for yy in np.linspace(y + h * 0.04, y + h * 0.96, 10)
                for xx in np.linspace(x + w * 0.04, x + w * 0.96, 15)
            ]
        )
        for ox, oy, ow, oh in obstacles:
            self.grid = self.grid[
                ~(
                    (self.grid[:, 0] >= ox)
                    & (self.grid[:, 0] <= ox + ow)
                    & (self.grid[:, 1] >= oy)
                    & (self.grid[:, 1] <= oy + oh)
                )
            ]

    def center(self, preferred=None):
        if not len(self.grid):
            raise ValueError("The reserved regions cover the entire drawing area")
        preferred = (
            self.grid.mean(axis=0) if preferred is None else np.asarray(preferred)
        )
        # The centroid of a non-convex free region may itself be reserved.
        for wall in self.obstacles:
            if np.all(preferred @ N.T <= wall + self.gap):
                return self.grid[
                    np.argmin(np.linalg.norm(self.grid - preferred, axis=1))
                ]
        return preferred

    def supports(self, positions, scale):
        return self.local * scale + (positions[self.owner] @ N.T)

    def holes(self, positions, scale):
        H = self.supports(positions, scale)
        distance = (self.grid @ N.T)[:, None, :] - H[None, :, :]
        distance = distance.max(axis=2)
        nearest = distance.argmin(axis=1)
        empty = np.maximum(0.0, distance[np.arange(len(distance)), nearest])
        # Fixed body outlines may be closest to an empty region, but cannot
        # move into it. Attract the nearest movable group instead.
        if np.any(~self.fixed):
            movable = distance.copy()
            movable[:, self.fixed[self.owner]] = np.inf
            nearest = movable.argmin(axis=1)
        return empty, self.owner[nearest]

    def center_composition(self, positions, scale):
        """Use the available sheet as a whole without changing attachments."""
        H = self.supports(positions, scale)
        lo = -H[:, [8, 12]].max(axis=0)
        hi = H[:, [0, 4]].max(axis=0)
        x, y, w, h = self.area
        low = np.array([x, y]) + self.gap - lo
        high = np.array([x + w, y + h]) - self.gap - hi
        for group, region in enumerate(self.regions):
            if self.fixed[group]:
                continue
            low = np.maximum(low, region[:2] - positions[group])
            high = np.minimum(high, region[:2] + region[2:] - positions[group])
        if np.any(low > high):
            return positions
        empty, _ = self.holes(positions, scale)
        best = float(np.mean(np.sort(empty)[-20:])), np.zeros(2)
        for dx in np.linspace(low[0], high[0], 9):
            for dy in np.linspace(low[1], high[1], 9):
                delta = np.array([dx, dy])
                shifted = H + N @ delta
                if any(
                    np.any((shifted + wall[OPPOSITE]).min(axis=1) + self.gap > 1e-5)
                    for wall in self.obstacles
                ):
                    continue
                empty, _ = self.holes(positions + delta, scale)
                score = float(np.mean(np.sort(empty)[-20:])) + 0.03 * np.linalg.norm(
                    delta
                )
                if score < best[0]:
                    best = score, delta
        return positions + best[1]

    def in_sector(self, group, position, origin):
        axis = self.cones[group]
        delta = position - origin
        region = self.regions[group]
        if np.any(position < region[:2] - 1e-7) or np.any(
            position > region[:2] + region[2:] + 1e-7
        ):
            return False
        if (
            self.rail_origins is not None
            and np.linalg.norm(position - self.rail_origins[group])
            > self.travel_radius[group] + 1e-7
        ):
            return False
        if (
            self.edit_origins is not None
            and np.linalg.norm(position - self.edit_origins[group])
            > self.edit_radius[group] + 1e-7
        ):
            return False
        if self.rail_origins is not None and np.linalg.norm(self.rails[group]) > 0:
            relative = position - self.rail_origins[group]
            if (
                abs(
                    self.rails[group, 0] * relative[1]
                    - self.rails[group, 1] * relative[0]
                )
                > 1e-5
            ):
                return False
        return (
            axis[2] < 0
            or float(delta @ axis[:2]) >= np.linalg.norm(delta) * axis[2] - 1e-7
        )

    def sector_position(self, group, position, origin):
        axis = self.cones[group]
        delta = position - origin
        length = np.linalg.norm(delta)
        if axis[2] < 0 or delta @ axis[:2] >= length * axis[2] - 1e-7 or length < 1e-8:
            return position
        perpendicular = np.array([-axis[1], axis[0]])
        side = 1 if delta @ perpendicular >= 0 else -1
        direction = axis[:2] * axis[2] + side * perpendicular * np.sqrt(
            1 - axis[2] ** 2
        )
        return origin + length * direction

    def repair(self, positions, scale):
        """Escape local contact traps by finding the nearest clear 2D move."""
        p = positions.copy()
        for group in np.flatnonzero(~self.fixed):
            region = self.regions[group]
            p[group] = np.clip(p[group], region[:2], region[:2] + region[2:])
            if self.edit_origins is not None:
                relative = p[group] - self.edit_origins[group]
                distance = np.linalg.norm(relative)
                if distance > self.edit_radius[group]:
                    p[group] = self.edit_origins[group] + relative * (
                        self.edit_radius[group] / distance
                    )
            relative = p[group] - self.rail_origins[group]
            distance = np.linalg.norm(relative)
            if distance > self.travel_radius[group]:
                p[group] = self.rail_origins[group] + relative * (
                    self.travel_radius[group] / distance
                )
            rail = self.rails[group]
            if np.linalg.norm(rail) > 0:
                origin = self.rail_origins[group]
                p[group] = origin + rail * ((p[group] - origin) @ rail)
            p[group] = self.sector_position(group, p[group], p[0])
        slices = np.split(np.arange(len(self.local)), np.cumsum(self.counts)[:-1])
        x, y, w, h = self.area
        for _ in range(2):
            for group in np.flatnonzero(~self.fixed):
                old_position = p[group].copy()
                if np.linalg.norm(self.rails[group]) > 0:
                    # Minimize attachment distance along the legal axis,
                    # including space opened by other groups during repair.
                    p[group] = self.rail_origins[group]
                H = self.supports(p, scale)
                own = H[slices[group]]
                other = np.concatenate([H[self.owner != group], self.obstacles])
                C = (
                    other[None, :, :] + own[:, None, OPPOSITE] + self.gap + 1e-4
                ).reshape(-1, 16)
                if np.all(C.min(axis=1) <= 0) and self.in_sector(group, p[group], p[0]):
                    continue
                best = None
                rail = self.rails[group]
                angles = (
                    [math.atan2(rail[1], rail[0]), math.atan2(-rail[1], -rail[0])]
                    if np.linalg.norm(rail) > 0
                    else np.arange(32) * (2 * np.pi / 32)
                )
                for angle in angles:
                    u = np.array([math.cos(angle), math.sin(angle)])
                    nu = N @ u
                    pos, neg = nu > 1e-9, nu < -1e-9
                    with np.errstate(divide="ignore", invalid="ignore"):
                        R = C / nu
                    upper = np.where(pos, R, np.inf).min(axis=1)
                    lower = np.where(neg, R, -np.inf).max(axis=1)
                    never = np.any(np.where(~(pos | neg), C, 1.0) < 0, axis=1)
                    valid = (lower < upper) & ~never & (upper > 0)
                    intervals = sorted(zip(lower[valid], upper[valid]))
                    distance = 0.0
                    if self.edit_origins is not None:
                        delta = self.edit_origins[group] - p[group]
                        along = delta @ u
                        radius2 = self.edit_radius[group] ** 2 - (
                            delta @ delta - along**2
                        )
                        if radius2 < 0:
                            continue
                        distance = max(0.0, along - math.sqrt(radius2))
                    for low, high in intervals:
                        if low <= distance < high:
                            distance = high
                    if not self.in_sector(group, p[group] + u * distance, p[0]):
                        continue
                    shifted = own + N @ (u * distance)
                    lo = -shifted[:, [8, 12]].max(axis=0)
                    hi = shifted[:, [0, 4]].max(axis=0)
                    if np.any(lo < [x + self.gap, y + self.gap]) or np.any(
                        hi > [x + w - self.gap, y + h - self.gap]
                    ):
                        continue
                    if best is None or distance < best[0]:
                        best = distance, u
                if best is not None:
                    p[group] += best[1] * best[0]
                else:
                    p[group] = old_position
        H = self.supports(p, scale)
        pair = H[self.a] + H[self.b][:, OPPOSITE]
        active = ~(self.fixed[self.ga] & self.fixed[self.gb])
        worst = max(0.0, float((pair.min(axis=1)[active] + self.gap).max(initial=0)))
        for wall in self.obstacles:
            worst = max(
                worst,
                float(((H + wall[OPPOSITE]).min(axis=1) + self.gap).max(initial=0)),
            )
        lo = -H[:, [8, 12]].max(axis=0)
        hi = H[:, [0, 4]].max(axis=0)
        worst = max(
            worst,
            float((np.array([x + self.gap, y + self.gap]) - lo).max()),
            float((hi - np.array([x + w - self.gap, y + h - self.gap])).max()),
        )
        for group in np.flatnonzero(~self.fixed):
            region = self.regions[group]
            worst = max(
                worst,
                float((region[:2] - p[group]).max()),
                float((p[group] - region[:2] - region[2:]).max()),
            )
            worst = max(
                worst,
                float(
                    np.linalg.norm(p[group] - self.rail_origins[group])
                    - self.travel_radius[group]
                ),
            )
            if self.edit_origins is not None:
                worst = max(
                    worst,
                    float(
                        np.linalg.norm(p[group] - self.edit_origins[group])
                        - self.edit_radius[group]
                    ),
                )
        return p, worst

    def relax(self, positions, scale, target, iterations=70):
        p = positions.copy()
        self.rail_origins = target
        self.travel_radius = self.max_travel * scale
        shape = self.local * scale
        pair_shape = shape[self.a] + shape[self.b][:, OPPOSITE]
        # Bounds of each compound's real constituent outlines.
        bounds = np.array(
            [
                [np.max(h[:, 0]), np.max(h[:, 4]), np.max(h[:, 8]), np.max(h[:, 12])]
                for h in np.split(shape, np.cumsum(self.counts)[:-1])
            ]
        )
        worst = math.inf
        for iteration in range(iterations):
            force = np.zeros_like(p)
            count = np.zeros(len(p))
            translation = p @ N.T
            C = pair_shape + translation[self.ga] - translation[self.gb]
            axes = C.argmin(axis=1)
            penetration = C[np.arange(len(C)), axes] + self.gap
            active = (penetration > 1e-6) & ~(self.fixed[self.ga] & self.fixed[self.gb])
            worst = max(0.0, float(penetration[active].max(initial=0)))
            if active.any():
                ga, gb = self.ga[active], self.gb[active]
                push = N[axes[active]] * penetration[active, None]
                both = ~self.fixed[ga] & ~self.fixed[gb]
                share_a = np.where(self.fixed[ga], 0.0, np.where(both, 0.5, 1.0))
                share_b = np.where(self.fixed[gb], 0.0, np.where(both, 0.5, 1.0))
                np.add.at(force, ga, -push * share_a[:, None])
                np.add.at(force, gb, push * share_b[:, None])
                np.add.at(count, ga, ~self.fixed[ga])
                np.add.at(count, gb, ~self.fixed[gb])
            H = shape + translation[self.owner]
            for wall in self.obstacles:
                C = H + wall[OPPOSITE]
                axes = C.argmin(axis=1)
                depth = C[np.arange(len(C)), axes] + self.gap
                active = depth > 1e-6
                worst = max(worst, float(depth.max(initial=0)))
                if active.any():
                    owner = self.owner[active]
                    np.add.at(force, owner, -N[axes[active]] * depth[active, None])
                    np.add.at(count, owner, 1)
            x, y, w, h = self.area
            margin = self.gap
            regions = self.regions
            lower = np.c_[x + margin + bounds[:, 2], y + margin + bounds[:, 3]]
            upper = np.c_[x + w - margin - bounds[:, 0], y + h - margin - bounds[:, 1]]
            limits = np.c_[
                np.maximum(lower, regions[:, :2]),
                np.minimum(upper, regions[:, :2] + regions[:, 2:]),
            ]
            correction = np.maximum(limits[:, :2] - p, 0) - np.maximum(
                p - limits[:, 2:], 0
            )
            worst = max(worst, float(np.abs(correction).max()))
            force += correction
            count += np.abs(correction).sum(axis=1) > 0
            force /= np.maximum(count[:, None], 1.0)
            # Weak elastic relationships keep groups close to their assembly
            # neighborhood. The final iterations enforce constraints alone.
            if iteration < iterations - 20:
                force += (target - p) * 0.009
                if iteration % 5 == 0:
                    distance, owner = self.holes(p, scale)
                    threshold = min(self.area[2:]) * 0.10
                    chosen = np.flatnonzero(distance > threshold)
                    for group in np.flatnonzero(~self.fixed):
                        nearby = chosen[owner[chosen] == group]
                        if len(nearby):
                            weights = distance[nearby] - threshold
                            vacancy = np.average(
                                self.grid[nearby], axis=0, weights=weights
                            )
                            force[group] += (vacancy - p[group]) * 0.04
                # Small neighboring groups stay beside their actual assembly
                # neighbor, rather than all orbiting the largest body.
                for child, parent in enumerate(self.parents):
                    if child == parent:
                        continue
                    pull = (
                        p[parent] + target[child] - target[parent] - p[child]
                    ) * 0.006
                    force[child] += pull
                    if not self.fixed[parent]:
                        force[parent] -= pull
            force[self.fixed] = 0
            p += force * 0.95
            if worst < 0.015 and iteration >= iterations - 20:
                break
        return self.repair(p, scale)


def pack(
    outlines,
    seeds,
    area,
    obstacles,
    spread=1.0,
    fixed=None,
    parents=None,
    cones=None,
    warm=None,
    clearance=0.0,
    attachments=None,
    rails=None,
    max_travel=math.inf,
    regions=None,
    region_edits=None,
):
    """Return group centers in sheet coordinates and a common drawing scale."""
    area = np.asarray(area, float)
    width, height = area[2:]
    fixed = np.zeros(len(outlines), bool) if fixed is None else np.asarray(fixed, bool)
    gap = max(1.4 + 0.8 * spread, clearance)
    parents = [0] * len(outlines) if parents is None else parents
    cones = np.tile([0, 0, -1], (len(outlines), 1)) if cones is None else cones
    rails = np.zeros((len(outlines), 2)) if rails is None else rails
    problem = Problem(outlines, area, obstacles, gap, fixed, parents, cones, rails)
    problem.max_travel = np.full(len(outlines), max_travel)
    if regions is not None:
        for i, region in enumerate(regions):
            if region is not None and not fixed[i]:
                rx, ry, rw, rh = region
                problem.regions[i] = [
                    area[0] + rx * width,
                    area[1] + ry * height,
                    rw * width,
                    rh * height,
                ]
                problem.cones[i] = [0, 0, -1]
                # An explicit user region supersedes the automatic estimate
                # of how far a group ought to travel from the assembly.
                problem.max_travel[i] = math.inf
    # A requested region change may move that group freely; neighboring
    # groups retain the small-edit displacement bound.
    if region_edits is not None:
        problem.edit_radius[np.asarray(region_edits, bool)] = math.inf
    seeds = np.asarray(seeds, float)
    attachments = (
        seeds[0] - seeds[0][0]
        if attachments is None
        else np.asarray(attachments, float)
    )
    best = None
    if warm is not None:
        previous, scale, old_area = warm
        ratio = min(area[2] / old_area[2], area[3] / old_area[3])
        scale *= ratio
        center = problem.center(
            area[:2] + (previous[0] - old_area[:2]) * (area[2:] / old_area[2:])
        )
        p = center + (previous - previous[0]) * ratio
        p[fixed] = center + scale * (seeds[0][fixed] - seeds[0][0])
        problem.edit_origins = p.copy()
        for _ in range(4):
            target = center + scale * attachments
            p, residual = problem.relax(p, scale, target, iterations=35)
            if residual < 0.035:
                trial = p.copy()
                trial[fixed] = center + scale * 1.02 * (seeds[0][fixed] - seeds[0][0])
                trial, error = problem.relax(
                    trial,
                    scale * 1.02,
                    center + scale * 1.02 * attachments,
                    iterations=35,
                )
                if error < 0.035:
                    p, scale = trial, scale * 1.02
                return p, scale
            scale *= 0.98
            p = problem.edit_origins.copy()
            p[fixed] = center + scale * (seeds[0][fixed] - seeds[0][0])
        problem.edit_origins = None
    for seed in seeds:
        # Start by fitting the initial composition. Optimization can change
        # both coordinates of every unpinned group, including its main body.
        H = np.concatenate(
            [
                h
                + (
                    seed[i]
                    @ np.c_[
                        np.cos(np.arange(64) * 2 * np.pi / 64),
                        np.sin(np.arange(64) * 2 * np.pi / 64),
                    ].T
                )
                for i, h in enumerate(outlines)
            ]
        )
        lo = -H[:, [32, 48]].max(axis=0)
        hi = H[:, [0, 16]].max(axis=0)
        scale = 0.94 * min(width / (hi[0] - lo[0]), height / (hi[1] - lo[1]))
        center = problem.center()
        p = center + scale * (seed - seed[0])
        # Keep the preferred composition compact rather than preserving long
        # greedy offsets. Contacts determine the final separation.
        target = center + scale * attachments
        p, residual = problem.relax(p, scale, target)
        for _ in range(8):
            if residual < 0.035:
                break
            scale *= 0.9
            p = center + 0.9 * (p - center)
            p[fixed] = center + scale * (seed[fixed] - seed[0])
            target = center + scale * attachments
            p, residual = problem.relax(p, scale, target)
        if os.environ.get("CADRAWS_DEBUG"):
            print("[joint] initial", scale, residual, flush=True)
        if residual >= 0.035:
            continue
        accepted = (scale, p.copy())
        step = 0.12
        for _ in range(14):
            candidate = scale * (1 + step)
            trial = p.copy()
            trial[fixed] = center + candidate * (seed[fixed] - seed[0])
            trial, residual = problem.relax(
                trial, candidate, center + candidate * attachments
            )
            if residual < 0.035:
                scale, p = candidate, trial
                accepted = scale, p.copy()
            else:
                step *= 0.5
                if step < 0.006:
                    break
        scale, p = accepted
        p = problem.center_composition(p, scale)
        holes, _ = problem.holes(p, scale)
        # Scale and the worst empty regions both matter. A distant outlier
        # should not win merely because its bounding rectangle is larger.
        empty = np.mean(np.sort(holes)[-20:])
        strain = np.linalg.norm(
            (p - p[problem.parents])
            - scale * (attachments - attachments[problem.parents]),
            axis=1,
        )
        attachment = np.sqrt(np.mean(strain[~fixed] ** 2)) if np.any(~fixed) else 0.0
        score = scale / (1 + (empty + 0.8 * attachment) / min(width, height))
        if os.environ.get("CADRAWS_DEBUG"):
            print(
                "[joint] candidate",
                scale,
                "empty",
                empty,
                "attachment",
                attachment,
                "score",
                score,
                flush=True,
            )
        if best is None or score > best[0]:
            best = score, scale, p
    if best is None:
        raise ValueError("Could not fit the part groups in the available area")
    return best[2], best[1]
