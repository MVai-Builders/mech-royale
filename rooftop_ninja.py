"""MVai Rooftop Ninja Bot - Builders Buildathon Mech Royale
Master Strategy:
1. High Ground Verticality: Climbs campus buildings (9-28m) to roost on roofs,
   breaking ground-bot navmesh and line of sight.
2. Complete Stealth: Silent movement, zero gunshot signatures to avoid alerting the 15-bot horde.
3. 45 HP/s Passive Regen: Lets natural healing top off health while ground bots fight each other and the storm.
4. Zone Leapfrog: Moves from roof to roof as the safe circle shrinks.
"""

import json
import math
import sys
from pathlib import Path
import mechfight as mf


def dist2d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm2d(dx, dy):
    d = math.hypot(dx, dy)
    if d < 1e-5:
        return 0.0, 0.0
    return dx / d, dy / d


class RooftopNinjaBot:
    name = "MVai-ApexPredator"

    def __init__(self, hello: mf.Hello):
        self.config = hello.get("config", {})
        self.loot_cfg = self.config.get("loot", {})
        self.pickup_radius = self.loot_cfg.get("pickup_radius", 4.0)
        
        # Load precomputed buildings.json
        b_file = Path(__file__).parent / "buildings.json"
        if b_file.exists():
            self.climbable_roofs = json.loads(b_file.read_text(encoding="utf-8"))
        else:
            self.climbable_roofs = []

        self.target_roof = None
        self.last_pos = None
        self.stuck_ticks = 0

    def find_best_roof(self, pos, zone):
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        
        candidates = []
        for r in self.climbable_roofs:
            d_zone = dist2d([r["cx"], r["cy"]], zc)
            if d_zone < zr * 0.75:
                d_me = dist2d([r["cx"], r["cy"]], pos)
                candidates.append((d_me, r))
                
        if not candidates:
            curr_r = zone.get("radius", 400.0)
            curr_c = zone.get("center", [0.0, 0.0])
            for r in self.climbable_roofs:
                if dist2d([r["cx"], r["cy"]], curr_c) < curr_r * 0.85:
                    d_me = dist2d([r["cx"], r["cy"]], pos)
                    candidates.append((d_me, r))
                    
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        grounded = me.get("grounded", True)
        energy = me.get("energy", 100)
        current_tick = obs["tick"]
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)

        action: mf.Action = {"tick": current_tick}

        # 1. Upgrade Handling (Max Defense)
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            best_idx = 0
            best_score = -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("desc", "")).lower()
                score = 0
                if "armor" in desc: score = 10
                elif "sensor" in desc: score = 9
                elif "hp" in desc or "repair" in desc: score = 8
                elif "speed" in desc: score = 5
                if score > best_score:
                    best_score = score
                    best_idx = idx
            action["pick"] = best_idx

        # 2. Pickups nearby
        for p in obs.get("pickups", []):
            if dist2d(p["pos"], pos) <= self.pickup_radius:
                if me["weapons"][1] is None:
                    action["take"] = p["id"]
                    break

        # 3. Elevation check: Are we on a rooftop?
        is_on_roof = (pz > 8.0) and grounded

        # 4. Target Roof Selection
        if self.target_roof:
            d_roof_zone = dist2d([self.target_roof["cx"], self.target_roof["cy"]], zc)
            if d_roof_zone > zr * 0.85:
                self.target_roof = None
                
        if not self.target_roof:
            self.target_roof = self.find_best_roof(pos, zone)

        move_x, move_y = 0.0, 0.0
        should_jump = False
        should_boost = False

        if is_on_roof:
            # We are safe on a roof! Hold position near the center of the roof
            if self.target_roof:
                d_to_rc = dist2d(pos, [self.target_roof["cx"], self.target_roof["cy"]])
                if d_to_rc > 6.0:
                    move_x, move_y = norm2d(self.target_roof["cx"] - px, self.target_roof["cy"] - py)
        elif self.target_roof:
            rx, ry = self.target_roof["cx"], self.target_roof["cy"]
            dx = rx - px
            dy = ry - py
            move_x, move_y = norm2d(dx, dy)

            # Check if close to the building perimeter
            is_near_building = (
                self.target_roof["min_x"] - 6.0 <= px <= self.target_roof["max_x"] + 6.0 and
                self.target_roof["min_y"] - 6.0 <= py <= self.target_roof["max_y"] + 6.0
            )

            if is_near_building:
                if grounded:
                    should_jump = True
                if energy > 20 and pz < self.target_roof["top"]:
                    should_boost = True  # Jet upward!
                move_x, move_y = norm2d(dx, dy)
            else:
                if not me.get("in_zone", True) or dist2d(pos, zc) > zr:
                    should_boost = True
        else:
            move_x, move_y = norm2d(zc[0] - px, zc[1] - py)
            if not me.get("in_zone", True):
                should_boost = True

        # Anti-stuck while ground travelling
        if not is_on_roof and self.last_pos is not None:
            if dist2d(pos, self.last_pos) < 0.1:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
            if self.stuck_ticks > 2:
                should_jump = True
                should_boost = True
                move_x, move_y = -move_y, move_x
        self.last_pos = pos

        # 5. Combat & Aim: Purely defensive
        # NEVER open fire at range - sound carries 320m and gives away position!
        fire = [False, False]
        aim_yaw = math.atan2(move_y, move_x) if (move_x or move_y) else 0.0
        aim_pitch = 0.0

        visible_enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        if visible_enemies:
            closest = min(visible_enemies, key=lambda m: dist2d(m["pos"], pos))
            edist = dist2d(closest["pos"], pos)
            # Only shoot if in point-blank range (< 18m)
            if edist < 18.0:
                ex, ey, ez = closest["pos"]
                aim_yaw = math.atan2(ey - py, ex - px)
                aim_pitch = math.atan2(ez - pz, max(1.0, edist))
                fire = [True, True]

        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=fire
        )
        return action


if __name__ == "__main__":
    mf.run(RooftopNinjaBot)
