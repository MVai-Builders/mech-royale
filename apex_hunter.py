"""MVai Apex Predator v9 - Builders Buildathon Mech Royale
=============================================================
Architecture: Data-Driven Master Bot
Key Systems:
  A. Precision Fire Discipline & Ballistics:
     - Free fire < 24m, lateral-lead gate 24-55m, hard cutoff > 55m.
     - Hit-feedback ceasefire & peek-sidestep (LOS building proxy).
     - Proactive calm reload.
  B. Sensor-Driven Laser Engine:
     - Uses real SDK w1["charge"] / weapons.laser.charge.
     - Hitscan release tick aiming directly at target torso with 0 lead.
  C. High-Yield Supply Drop Dominance:
     - 20s advance pathing, captive radius containment, full HP reset.
     - Swarm/low-HP abort rule.
  D. Anti-Rear Damage & Focus Fire:
     - Persistent sticky target (focuses fire to finish kills).
     - Torso locks to threat; search-turn toward sound bearing/last known position on unseen damage.
  E. Stochastic Evasion:
     - Randomized strafe reversal (3-9 ticks) and jump timing (15-35 ticks) to break enemy aim bots.
     - Incoming bullet geometric filter (ignores own bullets moving away).
  F. 16-Heading Context Steering:
     - Dynamically steers around learned obstacles, building walls, and storm boundaries.
  G. Token-Based Upgrade Priorities:
     - Armor & Damage scaling prioritized to melt early 1v1 house bots.
  H. Non-Intrusive Telemetry:
     - Logs Section 9 telemetry events to /tmp (never stdout) within hard 4.5 MB cap.
"""

import math
import random
import re
import sys
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf
from telemetry import TelemetryLogger, CombatTracker

# ----------------- Ballistic & Tactical Tunables -----------------
FIRE_FREE = 24.0          # Always fire inside this range (m) - travel time < 0.09s
FIRE_MAX = 55.0           # Do not fire gun beyond this range (wastes ammo & reveals position)
FIRE_MISS_MAX = 3.2       # Max lateral target displacement during flight time
BAND_LO, BAND_HI = 20.0, 32.0   # Preferred engagement distance (m)
DROP_PRE = 20.0           # Advance pathing time before drop lands (s)
WEAPON_RANGE = 60.0       # Max distance to scavenge secondary weapons
CUBE_RANGE = 35.0         # Max distance to vacuum power cubes
NPC_RANGE = 55.0          # Max distance to farm NPCs early
CRATE_RANGE = 45.0        # Max distance to smash crates
CELL_SIZE = 4.0           # Blocked-cell grid dimension (m)


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
        wc = cfg.get("weapons", {})
        mc = cfg.get("mech", {})

        self.reach = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.gun_speed = wc.get("gun", {}).get("speed", 252.0)
        self.laser_full_charge = wc.get("laser", {}).get("charge", 1.2)
        self.laser_min_charge = wc.get("laser", {}).get("min_charge", 0.4)
        self.slot = hello.get("slot", 0)
        self.rng = random.Random(1337 + int(self.slot))

        # Motion & Obstacle State
        self.last_pos = None
        self.last_cmd = (0.0, 0.0)
        self.last_heading = None
        self.stuck_ticks = 0
        self.blocked_cells: Dict[Tuple[int, int], float] = {}

        # Evasion State
        self.strafe_dir = 1
        self.flip_at_tick = 0
        self.jump_at_tick = 20
        self.last_hp = 1000.0
        self.dmg_tick = -999
        self.aim_prev_yaw = 0.0

        # Combat & Targeting State
        self.target_id: Optional[float] = None
        self.last_enemy_pos: Optional[List[float]] = None
        self.last_seen_tick = -999
        self.prev_enemy_hp: Dict[float, float] = {}
        self.saw_hp = False
        self.fired_ticks = deque()
        self.hit_ticks = deque()
        self.cease_until_tick = -1
        self.repos_until_tick = -1
        self.search_until_tick = -1
        self.search_yaw = 0.0
        self.match_tick = 0

        # Section 9 Telemetry
        self.logger = TelemetryLogger()
        self.tracker = CombatTracker(self.logger)

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

    # ────────────────────────── Upgrade Logic ──────────────────────────
    def _pick_upgrade(self, choices: List[Dict[str, Any]]) -> int:
        best_idx, best_val = 0, -1.0
        for i, c in enumerate(choices):
            text = (str(c.get("id", "")) + " " + str(c.get("name", "")) + " " + str(c.get("desc", ""))).lower()
            tokens = set(re.findall(r"[a-z]+", text))
            score = 3.0
            if "armor" in tokens or "armour" in tokens or "damage_taken" in tokens or "taken" in tokens:
                score = max(score, 12.0)
            if tokens & {"damage", "dmg", "rate", "firerate", "reload"}:
                score = max(score, 11.0)
            if tokens & {"repair", "hp", "health", "regen"}:
                score = max(score, 9.0)
            if tokens & {"speed", "boost", "energy"}:
                score = max(score, 7.0)
            if tokens & {"sensor", "vision", "range"}:
                score = max(score, 5.0)

            if score > best_val:
                best_idx, best_val = i, score
        return best_idx

    # ────────────────────────── Ballistics & Lead ──────────────────────
    def _lead_aim(self, target_pos: List[float], target_vel: List[float], my_pos: List[float]) -> Tuple[float, float]:
        tpx, tpy, tpz = target_pos
        tvx, tvy, tvz = target_vel
        mpx, mpy, mpz = my_pos
        lead_t = 0.0
        for _ in range(2):
            lx = tpx + tvx * lead_t
            ly = tpy + tvy * lead_t
            d = dist2d((lx, ly), (mpx, mpy))
            lead_t = min(0.50, d / max(1.0, self.gun_speed))

        fx = tpx + tvx * lead_t
        fy = tpy + tvy * lead_t
        fz = tpz + tvz * lead_t + 4.5  # Torso center-of-mass height
        yaw = math.atan2(fy - mpy, fx - mpx)
        hdist = max(1.0, dist2d((fx, fy), (mpx, mpy)))
        pitch = math.atan2(fz - (mpz + 7.0), hdist)
        return yaw, pitch

    def _solve_fire_gate(self, target: Dict[str, Any], my_pos: List[float]) -> Tuple[float, float, bool]:
        tpos = target["pos"]
        tvel = target.get("vel", [0.0, 0.0, 0.0])
        yaw, pitch = self._lead_aim(tpos, tvel, my_pos)
        d = target["_dist"]

        ux, uy = norm2d(tpos[0] - my_pos[0], tpos[1] - my_pos[1])
        # Lateral velocity relative to line of sight
        lateral_speed = abs(tvel[0] * uy - tvel[1] * ux)
        flight_time = d / max(1.0, self.gun_speed)
        lateral_displacement = lateral_speed * flight_time

        # Range gating
        gun_ok = (d <= FIRE_FREE) or (d <= FIRE_MAX and lateral_displacement <= FIRE_MISS_MAX)
        return yaw, pitch, gun_ok

    # ────────────────────────── Context Steering ────────────────────────
    def _repulsion_vector(self, my_pos: List[float], enemies: List[Dict[str, Any]], fallback: Tuple[float, float]) -> Tuple[float, float]:
        rx, ry = 0.0, 0.0
        for e in enemies:
            if e["_dist"] < 45.0:
                weight = 1.0 / max(6.0, e["_dist"])
                nx, ny = norm2d(my_pos[0] - e["pos"][0], my_pos[1] - e["pos"][1])
                rx += nx * weight
                ry += ny * weight
        rx, ry = norm2d(rx, ry)
        return (rx, ry) if (rx != 0.0 or ry != 0.0) else fallback

    def _heading_penalty(self, ux: float, uy: float, pos: List[float], tick: float, inside: bool, center: List[float], radius: float) -> float:
        penalty = 0.0
        # Probe forward at 3m and 7m for learned blocked obstacles
        for dist_probe, weight in ((3.0, 1.2), (7.0, 0.6)):
            cx = int((pos[0] + ux * dist_probe) // CELL_SIZE)
            cy = int((pos[1] + uy * dist_probe) // CELL_SIZE)
            t_blocked = self.blocked_cells.get((cx, cy))
            if t_blocked is not None and (tick - t_blocked < 400):
                penalty += weight

        # Heavy penalty for running into the contracting storm
        if inside:
            probe_pos = (pos[0] + ux * 10.0, pos[1] + uy * 10.0)
            if dist2d(probe_pos, center) > radius * 0.96:
                penalty += 1.8
        return penalty

    def _context_steer(self, wx: float, wy: float, pos: List[float], tick: float, inside: bool, center: List[float], radius: float) -> Tuple[float, float]:
        if abs(wx) + abs(wy) < 1e-6:
            self.last_heading = None
            return 0.0, 0.0

        wx, wy = norm2d(wx, wy)
        if self._heading_penalty(wx, wy, pos, tick, inside, center, radius) == 0.0:
            self.last_heading = (wx, wy)
            return wx, wy

        # Evaluate 16 candidate headings around the circle
        best_heading, best_score = (wx, wy), -1e9
        for i in range(16):
            angle = i * (math.pi / 8.0)
            ux, uy = math.cos(angle), math.sin(angle)
            score = (ux * wx + uy * wy) - self._heading_penalty(ux, uy, pos, tick, inside, center, radius)
            # Continuity bonus to avoid jittering
            if self.last_heading is not None:
                score += 0.15 * (ux * self.last_heading[0] + uy * self.last_heading[1])

            if score > best_score:
                best_heading, best_score = (ux, uy), score

        self.last_heading = best_heading
        return best_heading

    # ────────────────────────── Main Decision Loop ──────────────────────
    def act(self, obs: mf.Observation) -> mf.Action:
        self.match_tick += 1
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        current_tick = obs["tick"]
        match_time = obs.get("t", 0.0)

        hp = me.get("hp", 1000.0)
        max_hp = me.get("max_hp", 1000.0)
        energy = me.get("energy", 100.0)
        in_zone = me.get("in_zone", True)
        grounded = me.get("grounded", True)
        cubes = me.get("cubes", 0)
        weapons = me.get("weapons", [None, None])
        hp_ratio = hp / max(1.0, max_hp)

        self.tracker.record_tick_hp(current_tick, hp)
        action: mf.Action = {"tick": current_tick}

        # ─── 0. Event Telemetry & Reactive Evasion ─────────────────
        events = obs.get("events", [])
        for ev in events:
            etype = ev.get("type")
            if etype == "damaged":
                self.tracker.on_damage_taken(
                    ev.get("amount", 0.0),
                    bool(obs.get("mechs", [])),
                    ev.get("rear", False),
                    ev.get("weapon", "unknown"),
                    ev.get("by"),
                    current_tick,
                    match_time
                )
            elif etype == "kill":
                self.tracker.on_kill(current_tick, match_time, ev.get("target", 0.0))
            elif etype == "drop":
                self.tracker.on_drop_captured(ev.get("id", 0.0), ev.get("cubes", cubes), hp, current_tick, match_time)

        # Damage reaction & randomized strafe flipping
        hp_lost = self.last_hp - hp
        if hp_lost > 2.0:
            self.strafe_dir *= -1
            self.flip_at_tick = current_tick + self.rng.randint(3, 9)
            if in_zone:
                self.dmg_tick = current_tick
        self.last_hp = hp

        if current_tick >= self.flip_at_tick:
            self.strafe_dir *= -1
            self.flip_at_tick = current_tick + self.rng.randint(4, 10)

        if hp <= 0.0:
            self.tracker.on_death("damage", len([m for m in obs.get("mechs", []) if m.get("kind") == "mech" and dist2d(m["pos"], pos) < 40.0]), cubes, current_tick, match_time)

        # ─── 1. Upgrades ───────────────────────────────────────────
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            if choices:
                pick_idx = self._pick_upgrade(choices)
                action["pick"] = pick_idx
                self.logger.log_event("offer", {
                    "choices": [c.get("id", "") for c in choices],
                    "pick": pick_idx
                }, current_tick, match_time)

        # ─── 2. Stuck Detection & Blocked Learning ─────────────────
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if math.hypot(*self.last_cmd) > 0.5 and moved < 0.14:
                self.stuck_ticks += 1
                if self.stuck_ticks >= 3:
                    cell = (int((px + self.last_cmd[0] * 3.0) // CELL_SIZE), int((py + self.last_cmd[1] * 3.0) // CELL_SIZE))
                    self.blocked_cells[cell] = current_tick
                    if self.stuck_ticks == 3:
                        self.logger.log_event("stuck", {"pos": [round(v, 2) for v in pos], "duration": 3}, current_tick, match_time)
            else:
                self.stuck_ticks = 0
        self.last_pos = list(pos)

        # Periodically purge stale blocked memory
        if len(self.blocked_cells) > 400:
            self.blocked_cells = {k: v for k, v in self.blocked_cells.items() if (current_tick - v < 400)}

        # ─── 3. Weapon Looting in Reach ────────────────────────────
        all_pickups = obs.get("pickups", [])
        has_secondary = len(weapons) > 1 and weapons[1] is not None
        near_pickups = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        pref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}
        if not has_secondary and near_pickups:
            best_pickup = max(near_pickups, key=lambda p: pref.get(p.get("kind", ""), 0))
            if pref.get(best_pickup.get("kind", ""), 0) > 0:
                action["take"] = best_pickup["id"]

        # ─── 4. Zone Geometry ──────────────────────────────────────
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)

        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)
        outside_curr = dist_to_curr > (curr_radius * 0.96)

        # ─── 5. Entities & Perception ──────────────────────────────
        all_mechs = obs.get("mechs", [])
        enemy_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        for e in enemy_mechs:
            e["_dist"] = dist2d(e["pos"], pos)

        npcs = [m for m in all_mechs if m.get("kind") == "npc"]
        for n in npcs:
            n["_dist"] = dist2d(n["pos"], pos)
        npcs.sort(key=lambda n: n["_dist"])

        # Hit-Feedback & LOS Ceasefire Proxy:
        # House bots never damage each other, so ANY enemy HP drop is our damage!
        for e in enemy_mechs:
            eid = e.get("id")
            ehp = e.get("hp")
            if eid is not None and ehp is not None:
                self.saw_hp = True
                prev_h = self.prev_enemy_hp.get(eid)
                if prev_h is not None and (prev_h - ehp > 0.5):
                    self.hit_ticks.append(current_tick)
                    self.tracker.record_hit()
                self.prev_enemy_hp[eid] = ehp

        while self.fired_ticks and (current_tick - self.fired_ticks[0] > 30):
            self.fired_ticks.popleft()
        while self.hit_ticks and (current_tick - self.hit_ticks[0] > 30):
            self.hit_ticks.popleft()

        # If 10+ shots registered zero damage in 30 ticks: building/wall obstruction! Ceasefire and peek-sidestep!
        if self.saw_hp and len(self.fired_ticks) >= 10 and not self.hit_ticks and current_tick >= self.cease_until_tick:
            self.cease_until_tick = current_tick + 6
            self.repos_until_tick = current_tick + 12
            self.strafe_dir *= -1
            self.fired_ticks.clear()

        # Sticky target selection (focus fire to secure eliminations)
        def target_score(m: Dict[str, Any]) -> float:
            score = m["_dist"] + m.get("hp", 1000.0) * 0.02
            if m.get("id") == self.target_id:
                score -= 15.0  # Sticky focus bonus
            return score

        enemy_mechs.sort(key=target_score)
        primary_enemy = enemy_mechs[0] if enemy_mechs else None
        if primary_enemy is not None:
            self.target_id = primary_enemy.get("id")
            self.last_enemy_pos = list(primary_enemy["pos"])
            self.last_seen_tick = current_tick

        projectiles = obs.get("projectiles", [])
        sounds = obs.get("sounds", [])
        drops = obs.get("drops", [])
        cubes_on_ground = obs.get("cubes", [])
        crates = obs.get("crates", [])

        # ─── 6. Projectile Dodging (Geometric filter ignores own shots) ───
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0
        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            pspeed = math.hypot(pv[0], pv[1], pv[2])
            if pspeed < 1.0:
                continue

            dx = px - pp[0]
            dy = py - pp[1]
            # Dot product: if negative or zero, bullet is moving AWAY from us
            if (pv[0] * dx + pv[1] * dy) <= 0:
                continue

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

        # ─── 7. Strategic State Selection ──────────────────────────
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        gun_ok = False
        combat_target = None
        in_combat_mode = False
        mode = "orbit"

        # Supply Drop Anticipation (20s pre-routing; 100% full health repair + 3 cubes)
        best_drop, best_drop_dist = None, 1e9
        my_id = me.get("id")
        for d in drops:
            holder = d.get("holder")
            if holder is not None and holder != my_id:
                continue
            dd = dist2d(d["pos"], pos)
            lands_in = d.get("lands_in", 0.0)
            if lands_in is None:
                lands_in = 0.0
            # Drop within reachable flight time and not inside outer storm
            if lands_in <= DROP_PRE and (dd / 19.5 <= lands_in + 25.0) and (dist2d(d["pos"], curr_center) < curr_radius + 15.0) and (dd < best_drop_dist):
                best_drop, best_drop_dist = d, dd

        close_threats = [e for e in enemy_mechs if e["_dist"] < 15.0]

        # Calculate away and strafe vectors for combat
        away_vec = (0.0, 0.0)
        tx, ty = (0.0, 0.0)
        if primary_enemy is not None:
            away_vec = self._repulsion_vector(pos, enemy_mechs, norm2d(px - primary_enemy["pos"][0], py - primary_enemy["pos"][1]))
            tx, ty = rotate90(away_vec[0], away_vec[1], self.strafe_dir)

        # Peaceful scavenging options
        is_calm = not [e for e in enemy_mechs if e["_dist"] < 45.0]
        target_weapon = None
        if not has_secondary and is_calm and all_pickups:
            good_pickups = [p for p in all_pickups if pref.get(p.get("kind", ""), 0) > 1 and dist2d(p["pos"], pos) < WEAPON_RANGE]
            if good_pickups:
                target_weapon = max(good_pickups, key=lambda p: (pref.get(p.get("kind", ""), 0), -dist2d(p["pos"], pos)))

        near_cube = None
        if cubes_on_ground and is_calm:
            close_cubes = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < CUBE_RANGE]
            if close_cubes:
                near_cube = min(close_cubes, key=lambda c: dist2d(c["pos"], pos))

        # ── State A: Supply Drop Dominance ──
        # Drop takes absolute priority unless swarmed at low HP
        if best_drop is not None and not (len(close_threats) >= 2 and hp_ratio < 0.35):
            mode = "drop"
            dp = best_drop["pos"]
            dr = best_drop.get("radius", 8.0)
            dx_, dy_ = norm2d(dp[0] - px, dp[1] - py)
            dtx, dty = rotate90(dx_, dy_, self.strafe_dir)

            if primary_enemy is not None:
                aim_yaw, aim_pitch, gun_ok = self._solve_fire_gate(primary_enemy, pos)
                should_fire = True
                combat_target = primary_enemy
            elif best_drop_dist > 1.0:
                aim_yaw = math.atan2(dy_, dx_)
            else:
                aim_yaw = self.aim_prev_yaw

            if best_drop_dist > dr * 0.5:
                # Approach drop zone
                move_x, move_y = dx_ * 0.85 + dtx * 0.15, dy_ * 0.85 + dty * 0.15
            elif primary_enemy is not None:
                # Contest drop while fighting
                pull = 0.2 + 0.6 * min(1.0, best_drop_dist / max(1.0, dr))
                move_x, move_y = dtx * 0.80 + dx_ * pull, dty * 0.80 + dy_ * pull
            else:
                # Stand still inside drop radius to bank the 4s capture requirement!
                move_x, move_y = 0.0, 0.0

        # ── State B: Combat ──
        elif primary_enemy is not None:
            mode = "combat"
            in_combat_mode = True
            tdist = primary_enemy["_dist"]
            combat_target = primary_enemy
            aim_yaw, aim_pitch, gun_ok = self._solve_fire_gate(primary_enemy, pos)
            should_fire = True

            # Telemetry: first seen & start engagement
            self.tracker.on_enemy_seen(current_tick, match_time, len(enemy_mechs), tdist)
            self.tracker.start_engagement(
                primary_enemy["id"],
                hp,
                primary_enemy.get("hp", 1000.0),
                len([e for e in enemy_mechs if e["_dist"] < 40.0]),
                current_tick,
                match_time
            )

            n_close = len([e for e in enemy_mechs if e["_dist"] < 30.0])
            n_visible = len(enemy_mechs)

            if current_tick < self.repos_until_tick:
                # LOS block detected: strafe laterally and creep forward to peek around wall
                move_x, move_y = tx * 0.85 - away_vec[0] * 0.15, ty * 0.85 - away_vec[1] * 0.15
            elif n_close >= 2 and hp_ratio < 0.28:
                # Swarmed & low HP: backpedal towards zone
                move_x = away_vec[0] * 0.60 + to_zone_x * 0.40
                move_y = away_vec[1] * 0.60 + to_zone_y * 0.40
            elif tdist < BAND_LO:
                # Backpedal while strafing (keeps front armor facing enemy, NO 1.5x REAR DAMAGE)
                move_x = away_vec[0] * 0.75 + tx * 0.25
                move_y = away_vec[1] * 0.75 + ty * 0.25
            elif tdist <= BAND_HI:
                # Sweet-spot fighting band: 80% lateral circle-strafe
                move_x = tx * 0.80 + to_zone_x * 0.10 + away_vec[0] * 0.10
                move_y = ty * 0.80 + to_zone_y * 0.10 + away_vec[1] * 0.10
            elif n_visible == 1 and tdist < 70.0:
                # Lone easy bot: smoothly close in while strafing to finish the kill
                move_x = -away_vec[0] * 0.50 + tx * 0.40 + to_zone_x * 0.10
                move_y = -away_vec[1] * 0.50 + ty * 0.40 + to_zone_y * 0.10
            else:
                # Multiple enemies at range: hold ground, do not rush into the squad crossfire
                move_x = tx * 0.80 + to_zone_x * 0.20
                move_y = ty * 0.80 + to_zone_y * 0.20

        elif self.tracker.active_engagement is not None:
            self.tracker.end_engagement("disengaged", hp, 0.0, current_tick, match_time)

        # ── State C: Secondary Weapon Scavenge ──
        if mode == "orbit" and target_weapon is not None:
            mode = "weapon"
            wp = target_weapon["pos"]
            move_x, move_y = norm2d(wp[0] - px, wp[1] - py)
            aim_yaw = math.atan2(move_y, move_x)

        # ── State D: Power Cube Vacuum ──
        elif mode == "orbit" and near_cube is not None:
            mode = "cube"
            cp = near_cube["pos"]
            move_x, move_y = norm2d(cp[0] - px, cp[1] - py)
            aim_yaw = math.atan2(move_y, move_x)

        # ── State E: Early NPC Farming ──
        elif mode == "orbit" and npcs and (match_time < 170.0 or cubes < 8) and npcs[0]["_dist"] < NPC_RANGE:
            mode = "npc"
            npc = npcs[0]
            combat_target = npc
            aim_yaw, aim_pitch, gun_ok = self._solve_fire_gate(npc, pos)
            should_fire = True
            nx, ny = norm2d(px - npc["pos"][0], py - npc["pos"][1])
            ntx, nty = rotate90(nx, ny, self.strafe_dir)
            if npc["_dist"] < 15.0:
                move_x, move_y = nx * 0.6 + ntx * 0.4, ny * 0.6 + nty * 0.4
            else:
                move_x, move_y = -nx * 0.5 + ntx * 0.5, -ny * 0.5 + nty * 0.5

        # ── State F: Crate Smasher ──
        elif mode == "orbit" and crates and cubes < 6 and any(dist2d(c["pos"], pos) < CRATE_RANGE for c in crates):
            mode = "crate"
            close_crates = [c for c in crates if dist2d(c["pos"], pos) < CRATE_RANGE]
            c = min(close_crates, key=lambda c: dist2d(c["pos"], pos))
            cp = c["pos"]
            cd = dist2d(cp, pos)
            aim_yaw = math.atan2(cp[1] - py, cp[0] - px)
            aim_pitch = math.atan2(cp[2] + 2.0 - (pz + 7.0), max(1.0, cd))
            should_fire = True
            gun_ok = (cd < 35.0)
            move_x, move_y = norm2d(cp[0] - px, cp[1] - py)

        # ── State G: Safe Perimeter Orbit ──
        elif mode == "orbit":
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

        # ─── 8. Anti-Rear Damage: Search-Turn on Unseen Fire ────────
        # If damaged while no enemy is visible, turn torso towards sound or last known position!
        if primary_enemy is None and not should_fire:
            if current_tick - self.dmg_tick <= 4 and current_tick > self.search_until_tick:
                threat_yaw = None
                for s in sounds:
                    if s.get("dist") in ("near", "mid") and "bearing" in s:
                        threat_yaw = s["bearing"]
                        break
                if threat_yaw is None and self.last_enemy_pos is not None and (current_tick - self.last_seen_tick < 50):
                    threat_yaw = math.atan2(self.last_enemy_pos[1] - py, self.last_enemy_pos[0] - px)
                if threat_yaw is None:
                    threat_yaw = self.aim_prev_yaw + 2.4  # Spin torso ~137°

                self.search_yaw = threat_yaw
                self.search_until_tick = current_tick + 8

            if current_tick <= self.search_until_tick:
                aim_yaw = self.search_yaw

        # ─── 9. Dodge & Storm Blend ─────────────────────────────────
        if dodge_urgency > 0.05:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        if outside_curr:
            move_x = to_zone_x * 0.85 + move_x * 0.15
            move_y = to_zone_y * 0.85 + move_y * 0.15

        # Stuck override: initial 90° heading shift
        if self.stuck_ticks > 1:
            move_x, move_y = rotate90(move_x, move_y, self.strafe_dir)
            if self.stuck_ticks > 5:
                self.strafe_dir *= -1
                move_x, move_y = -move_x, -move_y

        # Context steering around learned obstacles & storm boundaries
        move_x, move_y = self._context_steer(move_x, move_y, pos, current_tick, not outside_curr, curr_center, curr_radius)
        self.last_cmd = (move_x, move_y)

        # ─── 10. Locomotion: Stochastic Jump & Smart Boost ──────────
        # Jump every 15-35 ticks (randomized) + on stuck (clears 1.2m curbs)
        should_jump = grounded and (self.stuck_ticks > 1 or current_tick >= self.jump_at_tick)
        if should_jump and self.stuck_ticks <= 1:
            self.jump_at_tick = current_tick + self.rng.randint(15, 35)

        tdist_c = combat_target.get("_dist", 99.0) if combat_target else 99.0
        should_boost = False
        if not in_zone or outside_curr:
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 20.0:
            should_boost = True
        elif mode == "combat" and tdist_c < 14.0 and energy > 25.0:
            should_boost = True
        elif mode == "combat" and hp_ratio < 0.30 and len(enemy_mechs) >= 2 and energy > 18.0:
            should_boost = True
        elif mode == "drop" and best_drop_dist > 15.0 and energy > 30.0:
            should_boost = True
        elif energy > 80.0 and dist_to_next > (zr * 0.65):
            should_boost = True

        # ─── 11. Weapons Fire Control ──────────────────────────────
        fire_slot0 = False
        fire_slot1 = False
        released_hitscan = False
        w0 = weapons[0] if len(weapons) > 0 else None
        w1 = weapons[1] if len(weapons) > 1 else None
        is_ceasefire = current_tick < self.cease_until_tick

        if should_fire and not is_ceasefire:
            # Slot 0: Arm Gun
            if w0 is not None:
                ammo = w0.get("ammo", 30)
                cd = w0.get("cooldown", 0.0)
                reloading = w0.get("reload", 0.0)
                if ammo <= 0 and reloading <= 0.0:
                    action["reload"] = True
                elif gun_ok and cd <= 0.0 and ammo > 0 and reloading <= 0.0:
                    # Conserve ammo: don't spray last 6 rounds past 40m
                    if not (ammo <= 6 and tdist_c > 40.0):
                        fire_slot0 = True
                        self.tracker.record_shot("gun", tdist_c)

            # Slot 1: Secondary Weapon (Precision Laser / Lightning / Mortar)
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                cd1 = w1.get("cooldown", 0.0)
                current_charge = w1.get("charge", 0.0)

                if w1_kind == "laser" and mode in ("combat", "drop"):
                    if cd1 > 0.0 or tdist_c > 85.0:
                        fire_slot1 = False
                    elif current_charge >= self.laser_full_charge:
                        # Full charge reached: release trigger for instant hitscan beam!
                        fire_slot1 = False
                        released_hitscan = True
                        self.tracker.record_shot("laser", tdist_c)
                    elif current_charge >= self.laser_min_charge and (tdist_c > 45.0 or self.last_seen_tick < current_tick - 1):
                        # Early release: target escaping LOS
                        fire_slot1 = False
                        released_hitscan = True
                        self.tracker.record_shot("laser", tdist_c)
                    else:
                        # Continue holding trigger to charge
                        fire_slot1 = True

                elif w1_kind == "lightning" and tdist_c < 45.0:
                    fire_slot1 = (cd1 <= 0.0)
                    if fire_slot1:
                        self.tracker.record_shot("lightning", tdist_c)

                elif w1_kind == "mortar" and (25.0 < tdist_c < 80.0):
                    fire_slot1 = (cd1 <= 0.0)
                    if fire_slot1:
                        self.tracker.record_shot("mortar", tdist_c)

                elif w1_kind == "gun":
                    fire_slot1 = (cd1 <= 0.0)
                    if fire_slot1:
                        self.tracker.record_shot("gun", tdist_c)

        if fire_slot0 and mode in ("combat", "drop") and primary_enemy is not None:
            self.fired_ticks.append(current_tick)

        # Hitscan beam aim: On release tick, aim directly at current target coords with ZERO lead!
        if released_hitscan and combat_target is not None:
            aim_yaw, aim_pitch = self._lead_aim(combat_target["pos"], [0.0, 0.0, 0.0], pos)

        # Proactive calm reload
        if (not should_fire and (current_tick - self.last_seen_tick > 20) and w0 is not None
                and w0.get("ammo", 30) < 22 and w0.get("reload", 0.0) <= 0.0):
            action["reload"] = True

        self.aim_prev_yaw = aim_yaw
        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )
        return action


if __name__ == "__main__":
    mf.run(ApexHunter)
