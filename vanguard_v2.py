"""MVai Vanguard v2 - High Placement Survival & Tactical Combat Bot
Features:
- Dynamic obstacle avoidance & anti-stuck ledge jumping
- Zone prediction & safe interior circle positioning
- Passive 45 HP/s regeneration management
- Predictive projectile leading (252 m/s gun speed)
- Defensive upgrade prioritization
"""

import math
import sys
import mechfight as mf


def dist2d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm2d(x, y):
    d = math.hypot(x, y)
    if d < 1e-5:
        return 0.0, 0.0
    return x / d, y / d


class MVaiVanguardV2:
    name = "MVai-Vanguard-v2"

    def __init__(self, hello: mf.Hello):
        self.config = hello.get("config", {})
        self.loot_cfg = self.config.get("loot", {})
        self.pickup_radius = self.loot_cfg.get("pickup_radius", 4.0)
        self.gun_speed = 252.0
        
        # State tracking
        self.last_pos = None
        self.stuck_ticks = 0
        self.stuck_heading_offset = 0.0
        self.ticks_since_damage = 100

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        vel = me.get("vel", [0, 0, 0])
        current_tick = obs["tick"]
        
        action: mf.Action = {"tick": current_tick}

        # 1. Damage & Regen Awareness
        events = obs.get("events", [])
        took_damage = False
        attacker_id = None
        for ev in events:
            if ev.get("type") == "damaged":
                took_damage = True
                attacker_id = ev.get("by")
                self.ticks_since_damage = 0
                break
        self.ticks_since_damage += 1

        # 2. Upgrade Choices (Max Survivability)
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            best_idx = 0
            best_score = -1
            for idx, c in enumerate(choices):
                cid = c.get("id", "").lower()
                desc = c.get("desc", "").lower()
                score = 0
                if "armor" in cid or "damage taken" in desc:
                    score = 10
                elif "sensor" in cid:
                    score = 8
                elif "hp" in cid or "health" in desc or "repair" in desc:
                    score = 7
                elif "speed" in desc or "boost" in desc:
                    score = 5
                elif "rate" in desc:
                    score = 4
                if score > best_score:
                    best_score = score
                    best_idx = idx
            action["pick"] = best_idx

        # 3. Weapon Looting & Pickup
        pickups = obs.get("pickups", [])
        near_pickups = [
            p for p in pickups
            if dist2d(p["pos"], pos) <= self.pickup_radius
        ]
        if me["weapons"][1] is None and near_pickups:
            # Prefer lightning or laser or mortar over gun
            pref = {"lightning": 4, "laser": 3, "mortar": 2, "gun": 1}
            sorted_pickups = sorted(
                near_pickups,
                key=lambda p: pref.get(p.get("kind"), 0),
                reverse=True
            )
            action["take"] = sorted_pickups[0]["id"]

        # 4. Anti-Stuck & Ledge Handling
        speed = math.hypot(vel[0], vel[1])
        if self.last_pos is not None:
            actual_move = dist2d(pos, self.last_pos)
            if actual_move < 0.15:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
                self.stuck_heading_offset = 0.0
        self.last_pos = pos

        should_jump = False
        if self.stuck_ticks > 2:
            should_jump = me.get("grounded", True)
            self.stuck_heading_offset = 0.8  # shift heading ~45 degrees

        # 5. Zone Navigation: Head towards safe interior of next circle
        zone = obs.get("zone", {})
        next_center = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        next_radius = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_radius = zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])

        d_next = dist2d(pos, next_center)
        d_curr = dist2d(pos, curr_center)

        # Primary vector towards next center
        to_zone_x, to_zone_y = norm2d(next_center[0] - px, next_center[1] - py)

        # Weight vector
        move_x, move_y = to_zone_x, to_zone_y

        # If stuck, apply offset angle
        if self.stuck_heading_offset != 0:
            cos_a = math.cos(self.stuck_heading_offset)
            sin_a = math.sin(self.stuck_heading_offset)
            move_x, move_y = move_x * cos_a - move_y * sin_a, move_x * sin_a + move_y * cos_a

        # 6. Enemy Awareness & Threat Evasion
        visible_mechs = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        closest_enemy = None
        min_enemy_dist = float("inf")
        for m in visible_mechs:
            d = dist2d(m["pos"], pos)
            if d < min_enemy_dist:
                min_enemy_dist = d
                closest_enemy = m

        # If enemy is dangerously close (< 50m) and we are safe in zone, evade
        if closest_enemy and min_enemy_dist < 50.0 and d_next < next_radius * 0.85:
            # Repulsion vector away from enemy
            rx, ry = norm2d(px - closest_enemy["pos"][0], py - closest_enemy["pos"][1])
            # Blend zone vector with repulsion
            move_x = move_x * 0.4 + rx * 0.6
            move_y = move_y * 0.4 + ry * 0.6
            move_x, move_y = norm2d(move_x, move_y)

        # Boost conditions: outside safe zone or taking fire
        in_safe_zone = me.get("in_zone", True) and (d_curr < curr_radius * 0.95)
        need_boost = (not in_safe_zone) or (took_damage and me.get("energy", 0) > 30)

        # 7. Aim & Tactical Fire
        aim_yaw = math.atan2(move_y, move_x) if (move_x or move_y) else 0.0
        aim_pitch = 0.0
        fire_slot0 = False
        fire_slot1 = False

        if closest_enemy:
            epos = closest_enemy["pos"]
            evel = closest_enemy.get("vel", [0, 0, 0])
            edist = min_enemy_dist

            # Lead the target using projectile velocity
            flight_time = edist / self.gun_speed
            lead_x = epos[0] + evel[0] * flight_time
            lead_y = epos[1] + evel[1] * flight_time
            lead_z = epos[2] + evel[2] * flight_time + 4.5  # torso center

            aim_yaw = math.atan2(lead_y - py, lead_x - px)
            aim_pitch = math.atan2(lead_z - (pz + 7.0), max(1.0, dist2d((lead_x, lead_y), pos)))

            # Tactical fire rules:
            # - If we took damage from someone, return fire!
            # - If enemy is in close combat range (< 40m)
            # - If enemy has low HP (< 300)
            enemy_hp = closest_enemy.get("hp", 1400)
            if took_damage or edist < 40.0 or enemy_hp < 300:
                fire_slot0 = True
                fire_slot1 = True

        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=need_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )
        return action


if __name__ == "__main__":
    mf.run(MVaiVanguardV2)
