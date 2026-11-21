"""MVai Apex Hunter v7.1 - Baseline v7 with Observe-Only Telemetry & Isolated Flags
==================================================================================
Identical to v7 (score 2.69) when all flags are False.
Each flag is strictly opt-in and testable one at a time.

Flags:
  F_DROP_BLEND   : When active, circles inside supply drop radius while fighting (no stand-still).
  F_LASER_CYCLE  : When active, releases laser trigger on reaching full charge or min_charge.
  F_SLOW_STRAFE  : When active, extends strafe duration to 20-30 ticks instead of rapid wiggling.
  F_UPGRADE_DMG  : When active, uses token-based regex upgrade priorities (Damage/Armor).
  F_SEARCH_TURN  : When active, turns torso toward sound/threat bearing when taking unseen rear damage.
"""

import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf
from telemetry import TelemetryLogger, CombatTracker

# ----------------- ISOLATED EXPERIMENT FLAGS (ALL OFF BY DEFAULT) -----------------
F_DROP_BLEND  = False
F_LASER_CYCLE = False
F_SLOW_STRAFE = False
F_UPGRADE_DMG = False
F_SEARCH_TURN = False
# ----------------------------------------------------------------------------------


def dist2d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm2d(dx: float, dy: float) -> Tuple[float, float]:
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0, 0.0
    return dx / d, dy / d


def rotate90(dx: float, dy: float, direction: int = 1) -> Tuple[float, float]:
    return -dy * direction, dx * direction


class ApexHunter:
    name = "MVai-Apex"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.reach = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.gun_speed = cfg.get("weapons", {}).get("gun", {}).get("speed", 252.0)
        self.laser_full_charge = cfg.get("weapons", {}).get("laser", {}).get("charge", 1.2)
        self.laser_min_charge = cfg.get("weapons", {}).get("laser", {}).get("min_charge", 0.4)
        self.slot = hello.get("slot", 0)

        # Baseline v7 state tracking
        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1
        self.last_hp = 1000.0
        self.match_tick = 0
        self.kills = 0

        # Telemetry logger (observe-only, capped, writes to /tmp/mech_tel_<pid>.jsonl)
        pid = os.getpid()
        log_path = f"/tmp/mech_tel_{pid}.jsonl" if os.path.exists("/tmp") and os.access("/tmp", os.W_OK) else f"mech_tel_{pid}.jsonl"
        self.logger = TelemetryLogger(log_path=log_path)
        self.tracker = CombatTracker(self.logger)
        self.prev_enemy_hp = {}

        # Flag-specific states
        self.strafe_flip_tick = 0
        self.search_until_tick = -1
        self.search_yaw = 0.0
        self.aim_prev_yaw = 0.0

    def __del__(self):
        try:
            self.logger.log_event("per_match_summary", {
                "kills": self.tracker.kills,
                "drops_captured": self.tracker.drops_captured,
                "final_tick": self.match_tick,
                "final_hp": round(self.last_hp, 1)
            })
        except Exception:
            pass

    def act(self, obs: mf.Observation) -> mf.Action:
        t0_start = time.perf_counter()
        self.match_tick += 1
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        hp = me.get("hp", 1000.0)
        max_hp = me.get("max_hp", 1000.0)
        energy = me.get("energy", 100.0)
        in_zone = me.get("in_zone", True)
        grounded = me.get("grounded", True)
        cubes = me.get("cubes", 0)
        weapons = me.get("weapons", [None, None])
        current_tick = obs["tick"]
        match_time = obs.get("t", 0.0)
        hp_ratio = hp / max(1.0, max_hp)

        # Telemetry tick recording
        self.tracker.record_tick_hp(current_tick, hp)
        action: mf.Action = {"tick": current_tick}

        # ─── 0. Telemetry & Event Handling ──────────────────────────
        events = obs.get("events", [])
        unseen_damage_taken = False
        damage_bearing = None
        for ev in events:
            etype = ev.get("type")
            if etype == "damaged":
                amount = ev.get("amount", 0.0)
                is_rear = ev.get("rear", False)
                has_visible_enemy = bool([m for m in obs.get("mechs", []) if m.get("kind") == "mech"])
                if not has_visible_enemy:
                    unseen_damage_taken = True
                    damage_bearing = ev.get("bearing")
                self.tracker.on_damage_taken(amount, has_visible_enemy, is_rear, ev.get("weapon", "unknown"), ev.get("by"), current_tick, match_time)
            elif etype == "kill":
                self.tracker.on_kill(current_tick, match_time, ev.get("target", 0.0))
            elif etype == "drop":
                self.tracker.on_drop_captured(ev.get("id", 0.0), ev.get("cubes", cubes), hp, current_tick, match_time)
            elif etype == "hit":
                self.tracker.record_hit()

        # Strafe reversal behavior
        hp_lost = self.last_hp - hp
        if hp_lost > 2.0:
            self.strafe_dir *= -1
            if F_SLOW_STRAFE:
                self.strafe_flip_tick = current_tick + 25

        if F_SLOW_STRAFE and current_tick >= self.strafe_flip_tick:
            self.strafe_dir *= -1
            self.strafe_flip_tick = current_tick + 25

        self.last_hp = hp
        if hp <= 0.0:
            self.tracker.on_death("damage", len([m for m in obs.get("mechs", []) if m.get("kind") == "mech" and dist2d(m["pos"], pos) < 40.0]), cubes, current_tick, match_time)

        # ─── 1. Upgrade Selection ───────────────────────────────────
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            pick_idx = 0
            if F_UPGRADE_DMG:
                import re
                best_val = -1.0
                for i, c in enumerate(choices):
                    text = (str(c.get("id", "")) + " " + str(c.get("name", "")) + " " + str(c.get("desc", ""))).lower()
                    tok = set(re.findall(r"[a-z]+", text))
                    sc = 3.0
                    if "armor" in tok or "armour" in tok or "taken" in tok: sc = max(sc, 12.0)
                    if tok & {"damage", "dmg", "rate", "firerate", "reload"}: sc = max(sc, 11.0)
                    if tok & {"repair", "hp", "health", "regen"}: sc = max(sc, 9.0)
                    if tok & {"speed", "boost", "energy"}: sc = max(sc, 7.0)
                    if tok & {"sensor", "vision", "range"}: sc = max(sc, 5.0)
                    if sc > best_val: best_val, pick_idx = sc, i
            else:
                # Pure baseline v7 priority
                priority = {
                    "armor": 12, "damage_taken": 12,
                    "sensor": 10, "vision": 10,
                    "repair": 9, "hp": 9, "health": 9, "regen": 9,
                    "speed": 7, "boost": 6, "energy": 6,
                    "rate": 5, "damage": 4, "range": 3, "reload": 2
                }
                best_val = -1
                for idx, c in enumerate(choices):
                    desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                    val = max((v for k, v in priority.items() if k in desc), default=3)
                    if val > best_val:
                        best_val, pick_idx = val, idx

            action["pick"] = pick_idx
            self.logger.log_event("offer", {"choices": [c.get("id", "") for c in choices], "pick": pick_idx}, current_tick, match_time)

        # ─── 2. Weapon Looting in Reach ────────────────────────────
        all_pickups = obs.get("pickups", [])
        near = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None
        if not has_secondary and near:
            pref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}
            best_pickup = max(near, key=lambda p: pref.get(p.get("kind", ""), 0))
            if pref.get(best_pickup.get("kind", ""), 0) > 0:
                action["take"] = best_pickup["id"]

        # ─── 3. Zone Navigation Geometry ───────────────────────────
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)

        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)
        outside_curr = dist_to_curr > (curr_radius * 0.96)

        # ─── 4. Entities & Perception ──────────────────────────────
        all_mechs = obs.get("mechs", [])
        enemy_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        for e in enemy_mechs:
            e["_dist"] = dist2d(e["pos"], pos)
        enemy_mechs.sort(key=lambda m: (m.get("hp", 1000.0) if m["_dist"] < 35.0 else m["_dist"] * 50.0))

        npcs = [m for m in all_mechs if m.get("kind") == "npc"]
        for n in npcs:
            n["_dist"] = dist2d(n["pos"], pos)
        npcs.sort(key=lambda n: n["_dist"])

        projectiles = obs.get("projectiles", [])
        sounds = obs.get("sounds", [])
        drops = obs.get("drops", [])
        cubes_on_ground = obs.get("cubes", [])
        crates = obs.get("crates", [])

        # ─── 5. Reactive Projectile Dodging ────────────────────────
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0
        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            pspeed = math.hypot(pv[0], pv[1], pv[2])
            if pspeed < 1.0: continue

            dx = px - pp[0]
            dy = py - pp[1]
            pdist = math.hypot(dx, dy)
            t_impact = pdist / pspeed

            if 0.0 < t_impact < 0.65:
                pred_bx = pp[0] + pv[0] * t_impact
                pred_by = pp[1] + pv[1] * t_impact
                miss_d = dist2d([pred_bx, pred_by], pos)

                if miss_d < 9.0:
                    b_dir_x, b_dir_y = norm2d(pv[0], pv[1])
                    perp_x, perp_y = rotate90(b_dir_x, b_dir_y, self.strafe_dir)
                    urgency = (1.0 - t_impact / 0.65) * (1.0 - miss_d / 9.0)
                    dodge_x += perp_x * urgency
                    dodge_y += perp_y * urgency
                    dodge_urgency = max(dodge_urgency, urgency)

        # ─── 6. Decision Branches (Strict v7 Hierarchy) ────────────
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None
        in_direct_combat = False

        # Supply drop evaluation
        best_drop = None
        for d in drops:
            ddist = dist2d(d["pos"], pos)
            lands_in = d.get("lands_in", 999.0)
            holder = d.get("holder")
            if lands_in <= 12.0 and (holder is None or holder == me.get("id")):
                if ddist < 90.0:
                    best_drop = d
                    break

        target_weapon_pickup = None
        if not has_secondary and all_pickups and not enemy_mechs:
            pref = {"laser": 4, "lightning": 3, "mortar": 2}
            good_weapons = [p for p in all_pickups if pref.get(p.get("kind", ""), 0) > 0 and dist2d(p["pos"], pos) < 55.0]
            if good_weapons:
                target_weapon_pickup = max(good_weapons, key=lambda p: pref.get(p.get("kind", ""), 0))

        near_cube = None
        if cubes_on_ground and not enemy_mechs:
            close_cubes = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 25.0]
            if close_cubes:
                near_cube = min(close_cubes, key=lambda c: dist2d(c["pos"], pos))

        # BRANCH 1: COMBAT (Highest Priority in v7)
        if enemy_mechs:
            in_direct_combat = True
            primary_enemy = enemy_mechs[0]
            combat_target = primary_enemy
            tpos = primary_enemy["pos"]
            tvel = primary_enemy.get("vel", [0, 0, 0])
            tdist = primary_enemy["_dist"]

            self.tracker.on_enemy_seen(current_tick, match_time, len(enemy_mechs), tdist)
            self.tracker.start_engagement(primary_enemy["id"], hp, primary_enemy.get("hp", 1000.0), len([e for e in enemy_mechs if e["_dist"] < 40.0]), current_tick, match_time)

            aim_yaw, aim_pitch = self._compute_lead_aim(tpos, tvel, pos)
            should_fire = True

            away_x, away_y = norm2d(px - tpos[0], py - tpos[1])
            tx, ty = rotate90(away_x, away_y, self.strafe_dir)

            if F_DROP_BLEND and best_drop is not None:
                # Contesting drop while fighting: orbit within drop radius
                dp = best_drop["pos"]
                dr = best_drop.get("radius", 8.0)
                dd_drop = dist2d(dp, pos)
                dx_drop, dy_drop = norm2d(dp[0] - px, dp[1] - py)
                pull = 0.2 + 0.6 * min(1.0, dd_drop / max(1.0, dr))
                move_x = tx * 0.70 + dx_drop * pull
                move_y = ty * 0.70 + dy_drop * pull
            elif len([e for e in enemy_mechs if e["_dist"] < 30.0]) >= 2 and hp_ratio < 0.25:
                move_x = away_x * 0.5 + to_zone_x * 0.5
                move_y = away_y * 0.5 + to_zone_y * 0.5
            elif tdist < 18.0:
                move_x = away_x * 0.75 + tx * 0.25
                move_y = away_y * 0.75 + ty * 0.25
            elif tdist <= 35.0:
                move_x = tx * 0.75 + to_zone_x * 0.20 + away_x * 0.05
                move_y = ty * 0.75 + to_zone_y * 0.20 + away_y * 0.05
            else:
                move_x = -away_x * 0.45 + tx * 0.35 + to_zone_x * 0.20
                move_y = -away_y * 0.45 + ty * 0.35 + to_zone_y * 0.20

        elif self.tracker.active_engagement is not None:
            self.tracker.end_engagement("disengaged", hp, 0.0, current_tick, match_time)

        # BRANCH 2: VALUE HARVESTING (when no enemies visible)
        if not in_direct_combat:
            if best_drop:
                dp = best_drop["pos"]
                dr = best_drop.get("radius", 8.0)
                dd_drop = dist2d(dp, pos)
                if F_DROP_BLEND and dd_drop < dr * 0.7:
                    # Move gently in circle within radius (never stand dead still)
                    dx_d, dy_d = norm2d(dp[0] - px, dp[1] - py)
                    dtx, dty = rotate90(dx_d, dy_d, self.strafe_dir)
                    move_x, move_y = dtx * 0.5 + dx_d * 0.2, dty * 0.5 + dy_d * 0.2
                else:
                    move_x, move_y = norm2d(dp[0] - px, dp[1] - py)
                aim_yaw = math.atan2(move_y, move_x)

            elif target_weapon_pickup:
                wp = target_weapon_pickup["pos"]
                move_x, move_y = norm2d(wp[0] - px, wp[1] - py)
                aim_yaw = math.atan2(move_y, move_x)

            elif near_cube:
                cp = near_cube["pos"]
                move_x, move_y = norm2d(cp[0] - px, cp[1] - py)
                aim_yaw = math.atan2(move_y, move_x)

            elif npcs and (match_time < 160.0 or cubes < 6) and npcs[0]["_dist"] < 45.0:
                npc = npcs[0]
                combat_target = npc
                aim_yaw, aim_pitch = self._compute_lead_aim(npc["pos"], npc.get("vel", [0, 0, 0]), pos)
                should_fire = True
                nx, ny = norm2d(px - npc["pos"][0], py - npc["pos"][1])
                ntx, nty = rotate90(nx, ny, self.strafe_dir)
                if npc["_dist"] < 15.0:
                    move_x, move_y = nx * 0.6 + ntx * 0.4, ny * 0.6 + nty * 0.4
                else:
                    move_x, move_y = -nx * 0.5 + ntx * 0.5, -ny * 0.5 + nty * 0.5

            elif crates and cubes < 4:
                close_crates = [c for c in crates if dist2d(c["pos"], pos) < 30.0]
                if close_crates:
                    c = close_crates[0]
                    cp = c["pos"]
                    aim_yaw = math.atan2(cp[1] - py, cp[0] - px)
                    aim_pitch = math.atan2(cp[2] + 2.0 - (pz + 7.0), max(1.0, dist2d(cp, pos)))
                    should_fire = True
                    move_x, move_y = norm2d(cp[0] - px, cp[1] - py)

            else:
                # Perimeter 65% orbit
                if dist_to_next < (zr * 0.35):
                    out_x, out_y = norm2d(px - zc[0], py - zc[1])
                    tx, ty = rotate90(out_x, out_y, self.strafe_dir)
                    move_x, move_y = out_x * 0.6 + tx * 0.4, out_y * 0.6 + ty * 0.4
                elif dist_to_next > (zr * 0.85):
                    move_x, move_y = to_zone_x, to_zone_y
                else:
                    tx, ty = rotate90(to_zone_x, to_zone_y, self.strafe_dir)
                    move_x, move_y = tx * 0.80 + to_zone_x * 0.20, ty * 0.80 + to_zone_y * 0.20

                if sounds:
                    for s in sounds:
                        if s.get("dist") in ("near", "mid") and s.get("kind") in ("gun", "mortar", "laser", "lightning"):
                            aim_yaw = s.get("bearing", aim_yaw)
                            break

                # F_SEARCH_TURN: Orient toward unseen damage
                if F_SEARCH_TURN and unseen_damage_taken:
                    if damage_bearing is not None:
                        self.search_yaw = damage_bearing
                    else:
                        self.search_yaw = self.aim_prev_yaw + 2.4
                    self.search_until_tick = current_tick + 6

                if F_SEARCH_TURN and current_tick <= self.search_until_tick:
                    aim_yaw = self.search_yaw

        # ─── 7. Dodge & Storm Integration ──────────────────────────
        if dodge_urgency > 0.05:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        if outside_curr:
            move_x = to_zone_x * 0.85 + move_x * 0.15
            move_y = to_zone_y * 0.85 + move_y * 0.15

        # ─── 8. Anti-Stuck & Curb Leaping ───────────────────────────
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if moved < 0.14:
                self.stuck_ticks += 1
                if self.stuck_ticks == 3:
                    self.logger.log_event("stuck", {"pos": [round(v, 2) for v in pos], "duration": 3}, current_tick, match_time)
            else:
                self.stuck_ticks = 0
        self.last_pos = list(pos)

        if self.stuck_ticks > 1:
            move_x, move_y = rotate90(move_x, move_y, self.strafe_dir)
            if self.stuck_ticks > 5:
                self.strafe_dir *= -1
                move_x, move_y = -move_x, -move_y

        move_x, move_y = norm2d(move_x, move_y)
        should_jump = ((current_tick % 25 == 0) or (self.stuck_ticks > 1)) and grounded

        # Boost
        should_boost = False
        if not in_zone or outside_curr:
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 20.0:
            should_boost = True
        elif in_direct_combat and combat_target and combat_target.get("_dist", 50.0) < 14.0 and energy > 25.0:
            should_boost = True
        elif best_drop and energy > 65.0 and dist2d(best_drop["pos"], pos) > 20.0:
            should_boost = True
        elif energy > 80.0 and dist_to_next > (zr * 0.65):
            should_boost = True

        # ─── 9. Weapons Fire ───────────────────────────────────────
        fire_slot0 = False
        fire_slot1 = False
        released_hitscan = False

        if should_fire:
            w0 = weapons[0] if len(weapons) > 0 else None
            tdist = combat_target.get("_dist", 50.0) if combat_target else 50.0
            if w0 is not None:
                ammo = w0.get("ammo", 30)
                cd = w0.get("cooldown", 0.0)
                reloading = w0.get("reload", 0.0)
                if cd <= 0.0 and ammo > 0 and reloading <= 0.0:
                    fire_slot0 = True
                    self.tracker.record_shot("gun", tdist)
                elif ammo <= 0 and reloading <= 0.0:
                    action["reload"] = True

            w1 = weapons[1] if len(weapons) > 1 else None
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                cd1 = w1.get("cooldown", 0.0)

                if w1_kind == "laser":
                    if F_LASER_CYCLE:
                        charge = w1.get("charge", 0.0)
                        if cd1 > 0.0:
                            fire_slot1 = False
                        elif charge >= self.laser_full_charge:
                            fire_slot1 = False
                            released_hitscan = True
                            self.tracker.record_shot("laser", tdist)
                        elif charge >= self.laser_min_charge and tdist > 40.0:
                            fire_slot1 = False
                            released_hitscan = True
                            self.tracker.record_shot("laser", tdist)
                        else:
                            fire_slot1 = True
                    else:
                        fire_slot1 = True
                        self.tracker.record_shot("laser", tdist)

                elif w1_kind == "lightning" and tdist < 45.0:
                    fire_slot1 = (cd1 <= 0.0)
                    if fire_slot1: self.tracker.record_shot("lightning", tdist)
                elif w1_kind == "mortar" and 25.0 < tdist < 85.0:
                    fire_slot1 = (cd1 <= 0.0)
                    if fire_slot1: self.tracker.record_shot("mortar", tdist)
                elif w1_kind == "gun":
                    fire_slot1 = (cd1 <= 0.0)
                    if fire_slot1: self.tracker.record_shot("gun", tdist)

        if released_hitscan and combat_target is not None:
            aim_yaw, aim_pitch = self._compute_lead_aim(combat_target["pos"], [0.0, 0.0, 0.0], pos)

        self.aim_prev_yaw = aim_yaw
        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )

        t_elapsed = (time.perf_counter() - t0_start) * 1000.0
        if t_elapsed > 10.0:
            self.logger.log_event("slow_tick", {"tick": current_tick, "latency_ms": round(t_elapsed, 2)})

        return action

    def _compute_lead_aim(self, target_pos: List[float], target_vel: List[float], my_pos: List[float]) -> Tuple[float, float]:
        tpx, tpy, tpz = target_pos
        tvx, tvy, tvz = target_vel
        mpx, mpy, mpz = my_pos
        lead_t = 0.0
        for _ in range(2):
            lx = tpx + tvx * lead_t
            ly = tpy + tvy * lead_t
            d = dist2d((lx, ly), (mpx, mpy))
            lead_t = min(0.50, d / max(1.0, self.gun_speed))

        final_x = tpx + tvx * lead_t
        final_y = tpy + tvy * lead_t
        final_z = tpz + tvz * lead_t + 4.5
        yaw = math.atan2(final_y - mpy, final_x - mpx)
        hdist = max(1.0, dist2d((final_x, final_y), (mpx, mpy)))
        pitch = math.atan2(final_z - (mpz + 7.0), hdist)
        return yaw, pitch


if __name__ == "__main__":
    mf.run(ApexHunter)
