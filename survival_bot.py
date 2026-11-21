"""MVai Survival Mech Bot - Builders Buildathon
Optimized for tactical perimeter positioning, stealth evasion, and survival placement.
"""

import math
import sys
import mechfight as mf


def norm_vec(dx: float, dy: float) -> tuple[float, float]:
    d = math.hypot(dx, dy)
    if d < 1e-5:
        return 0.0, 0.0
    return dx / d, dy / d


class MVaiSurvivalBot:
    name = "MVai-Vanguard"

    def __init__(self, hello: mf.Hello):
        self.config = hello.get("config", {})
        self.loot_cfg = self.config.get("loot", {})
        self.pickup_radius = self.loot_cfg.get("pickup_radius", 4.0)

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        px, py, pz = me["pos"]
        current_tick = obs["tick"]
        action: mf.Action = {"tick": current_tick}

        # 1. Handle upgrade offers - prioritize defensive survivability
        if obs.get("offer"):
            offer = obs["offer"]
            choices = offer.get("choices", [])
            pick_idx = 0
            for idx, c in enumerate(choices):
                cid = c.get("id", "")
                if any(k in cid for k in ("armor", "sensor", "hp", "shield")):
                    pick_idx = idx
                    break
            action["pick"] = pick_idx

        # 2. Weapon & Loot management
        near_pickups = [
            p for p in obs.get("pickups", [])
            if math.hypot(p["pos"][0] - px, p["pos"][1] - py) <= self.pickup_radius
        ]
        has_secondary = me["weapons"][1] is not None
        if not has_secondary and near_pickups:
            best_pickup = next((p for p in near_pickups if p.get("kind") != "gun"), near_pickups[0])
            action["take"] = best_pickup["id"]

        # 3. Zone Navigation
        zone = obs.get("zone", {})
        center = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        radius = zone.get("next_radius") or zone.get("radius", 300.0)

        # Vector to zone center
        to_center_x = center[0] - px
        to_center_y = center[1] - py
        dist_to_center = math.hypot(to_center_x, to_center_y)

        # Optimal target position is along the safe perimeter (around 65% of current radius)
        target_dist = radius * 0.65
        desired_move_x, desired_move_y = 0.0, 0.0

        if dist_to_center > radius * 0.9:
            # We are near or outside zone boundary: head inward
            nx, ny = norm_vec(to_center_x, to_center_y)
            desired_move_x += nx * 1.5
            desired_move_y += ny * 1.5
        elif dist_to_center < radius * 0.3:
            # Too close to center (bot hotspot): push outward toward perimeter
            nx, ny = norm_vec(px - center[0], py - center[1])
            desired_move_x += nx * 0.8
            desired_move_y += ny * 0.8

        # 4. Enemy avoidance (Repulsion field)
        visible_mechs = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        for enemy in visible_mechs:
            epos = enemy["pos"]
            edx = px - epos[0]
            edy = py - epos[1]
            edist = math.hypot(edx, edy)
            if edist < 80.0:
                weight = (80.0 - edist) / 80.0
                r_dx, r_dy = norm_vec(edx, edy)
                desired_move_x += r_dx * weight * 2.0
                desired_move_y += r_dy * weight * 2.0

        # Also account for recent directional sounds (shots / explosions)
        for s in obs.get("sounds", []):
            if s.get("dist") in ("near", "mid") and s.get("age", 1.0) < 0.2:
                bearing = s.get("bearing", 0.0)
                # Opposite of bearing
                desired_move_x += -math.cos(bearing) * 0.5
                desired_move_y += -math.sin(bearing) * 0.5

        # Normalize final movement vector
        move_x, move_y = norm_vec(desired_move_x, desired_move_y)
        if move_x == 0.0 and move_y == 0.0:
            move_x, move_y = norm_vec(to_center_x, to_center_y)

        # 5. Aim & Tactical Defense
        yaw = math.atan2(move_y, move_x) if (move_x or move_y) else 0.0
        pitch = 0.0
        should_fire = False

        if visible_mechs:
            closest_enemy = min(
                visible_mechs,
                key=lambda m: math.hypot(m["pos"][0] - px, m["pos"][1] - py)
            )
            ex, ey, ez = closest_enemy["pos"]
            dx = ex - px
            dy = ey - py
            hdist = math.hypot(dx, dy)
            target_yaw = math.atan2(dy, dx)
            target_pitch = math.atan2(ez - pz, max(1.0, hdist))

            # Aim toward threat
            yaw = target_yaw
            pitch = target_pitch

            # Only open fire if in reasonable range (< 60m)
            if hdist < 60.0:
                should_fire = True

        # Boost if outside safe zone or taking rear damage
        need_boost = not me.get("in_zone", True)
        for event in obs.get("events", []):
            if event.get("type") == "damaged":
                need_boost = True
                break

        action.update(
            move=[move_x, move_y],
            aim=[yaw, pitch],
            boost=need_boost,
            jump=False,
            fire=[should_fire, should_fire]
        )
        return action


if __name__ == "__main__":
    mf.run(MVaiSurvivalBot)
