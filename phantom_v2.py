"""MVai Phantom v2 — Master Stealth & Survival AI for Mech Royale
================================================================
Engineered specifically to counter the 15-bot practice squad and achieve top placements:
1. Stealth & Selective Engagement: Never shoot unless attacked or in defensive range (gun sound travels 320m).
2. Regen Exploitation: After taking damage, disengage to activate the 45 HP/s passive regeneration (5s cooldown).
3. Outer-Ring Safe Orbit: Position at 65-80% safe zone radius to avoid the central 15-bot crossfire death pit.
4. Relative-Vector Projectile Dodging: Perpendicular evasion against incoming bullets and mortar shells.
5. Early NPC Farming: Safely kill isolated NPCs in first 120s for +4% HP / +4% DMG power cubes.
6. Proper Laser Cycling: Full charge (0.75s) -> release instant 315-damage hitscan -> full-speed evasion during cooldown.
7. Anti-Stuck & Curb Clearance: Automatic 1.2m curb leaping and stuck rotation.
"""

import math
import sys
import mechfight as mf


def dist2d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm2d(dx: float, dy: float):
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0, 0.0
    return dx / d, dy / d


def rotate90(dx: float, dy: float, direction: int = 1):
    """Rotate 2D vector 90 degrees (+1 for CCW, -1 for CW)."""
    return -dy * direction, dx * direction


class PhantomV2:
    name = "MVai-Phantom"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        loot_cfg = cfg.get("loot", {})
        self.reach = loot_cfg.get("pickup_radius", 4.0)

        weap_cfg = cfg.get("weapons", {})
        self.gun_speed = weap_cfg.get("gun", {}).get("speed", 252.0)
        self.laser_charge_time = weap_cfg.get("laser", {}).get("charge", 0.75)

        self.slot = hello.get("slot", 0)

        # State tracking
        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1
        self.match_tick = 0
        self.last_hp = 1400.0
        self.last_damage_tick = -100

    def act(self, obs: mf.Observation) -> mf.Action:
        self.match_tick += 1
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        hp = me.get("hp", 1400.0)
        max_hp = me.get("max_hp", 1400.0)
        energy = me.get("energy", 100.0)
        in_zone = me.get("in_zone", True)
        grounded = me.get("grounded", True)
        weapons = me.get("weapons", [None, None])
        cubes = me.get("cubes", 0)
        current_tick = obs["tick"]
        match_time = obs.get("t", 0.0)
        hp_ratio = hp / max(1.0, max_hp)

        action: mf.Action = {"tick": current_tick}

        # ─── 1. Damage & Event Handling ────────────────────────────────
        events = obs.get("events", [])
        took_damage = False
        damage_amount = 0.0
        for ev in events:
            if ev.get("type") == "damaged":
                took_damage = True
                damage_amount += ev.get("amount", 0.0)
                self.last_damage_tick = current_tick

        # Flip strafe direction on significant hit
        if took_damage and damage_amount > 15.0:
            self.strafe_dir *= -1
        self.last_hp = hp

        ticks_since_damage = current_tick - self.last_damage_tick

        # ─── 2. Upgrade Selection (Priority: Armor > Sensors > HP/Regen) ──
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority = {
                "armor": 15, "damage_taken": 15,
                "sensor": 13, "vision": 13,
                "repair": 11, "regen": 11,
                "hp": 10, "health": 10,
                "speed": 8, "boost": 7, "energy": 7,
                "rate": 6, "damage": 5, "reload": 4
            }
            best_idx, best_val = 0, -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                val = max((v for k, v in priority.items() if k in desc), default=3)
                if val > best_val:
                    best_val, best_idx = val, idx
            action["pick"] = best_idx

        # ─── 3. Weapon Looting (Laser > Lightning > Mortar) ────────────
        all_pickups = obs.get("pickups", [])
        near_pickups = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None
        if not has_secondary and near_pickups:
            wpref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 0}
            best_p = max(near_pickups, key=lambda p: wpref.get(p.get("kind", ""), 0))
            if wpref.get(best_p.get("kind", ""), 0) > 0:
                action["take"] = best_p["id"]

        # ─── 4. Zone Geometry ──────────────────────────────────────────
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)

        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)
        outside_curr = dist_to_curr > (curr_radius * 0.96)

        # ─── 5. Entities & Perception ──────────────────────────────────
        all_mechs = obs.get("mechs", [])
        enemy_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        for e in enemy_mechs:
            e["_dist"] = dist2d(e["pos"], pos)
        enemy_mechs.sort(key=lambda m: m["_dist"])

        npcs = [m for m in all_mechs if m.get("kind") == "npc"]
        for n in npcs:
            n["_dist"] = dist2d(n["pos"], pos)
        npcs.sort(key=lambda n: n["_dist"])

        projectiles = obs.get("projectiles", [])
        sounds = obs.get("sounds", [])
        drops = obs.get("drops", [])
        crates = obs.get("crates", [])

        enemies_close = [e for e in enemy_mechs if e["_dist"] < 30.0]
        enemies_mid = [e for e in enemy_mechs if 30.0 <= e["_dist"] < 65.0]

        # ─── 6. Relative-Vector Projectile Dodging ──────────────────────
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0

        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            pkind = proj.get("kind", "gun")

            pspeed = math.hypot(pv[0], pv[1], pv[2])
            if pspeed < 1.0:
                continue

            dx = px - pp[0]
            dy = py - pp[1]
            pdist = math.hypot(dx, dy)
            t_impact = pdist / pspeed

            # Only react to incoming threats arriving within 0.7 seconds
            if 0.0 < t_impact < 0.70:
                pred_x = pp[0] + pv[0] * t_impact
                pred_y = pp[1] + pv[1] * t_impact
                miss_d = dist2d([pred_x, pred_y], pos)

                if miss_d < 10.0:
                    pv_nx, pv_ny = norm2d(pv[0], pv[1])
                    perp_x, perp_y = rotate90(pv_nx, pv_ny, self.strafe_dir)
                    urgency = (1.0 - t_impact / 0.70) * (1.0 - miss_d / 10.0)
                    if pkind == "mortar":
                        urgency *= 2.0
                    dodge_x += perp_x * urgency
                    dodge_y += perp_y * urgency
                    dodge_urgency = max(dodge_urgency, urgency)

        # ─── 7. Tactical Decision Hierarchy ────────────────────────────
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None

        # Disengagement conditions (Stealth Survival):
        # Disengage if low HP, or multiple enemies, or recently damaged and trying to trigger passive 45 HP/s regen
        should_flee = False
        if hp_ratio < 0.40:
            should_flee = True
        elif len(enemies_close) >= 2:
            should_flee = True
        elif len(enemies_close) >= 1 and hp_ratio < 0.60:
            should_flee = True
        elif took_damage and len(enemy_mechs) > 1:
            should_flee = True

        # CASE A: FLEE / DISENGAGE (Prioritize Survival & Regen)
        if should_flee and enemy_mechs:
            nearest = enemy_mechs[0]
            away_x, away_y = norm2d(px - nearest["pos"][0], py - nearest["pos"][1])
            tang_x, tang_y = rotate90(away_x, away_y, self.strafe_dir)

            if outside_curr:
                move_x = away_x * 0.35 + to_zone_x * 0.65
                move_y = away_y * 0.35 + to_zone_y * 0.65
            else:
                move_x = away_x * 0.65 + tang_x * 0.35
                move_y = away_y * 0.65 + tang_y * 0.35

            # Defensive fire only if enemy is very close (< 25m)
            if nearest["_dist"] < 25.0:
                combat_target = nearest
                aim_yaw, aim_pitch = self._lead_aim(nearest, pos)
                should_fire = True

        # CASE B: DEFENSIVE COMBAT (Only when forced or clean 1v1)
        elif enemy_mechs and (len(enemies_close) > 0 or (len(enemy_mechs) == 1 and enemy_mechs[0]["_dist"] < 45.0)):
            target = enemy_mechs[0]
            tdist = target["_dist"]
            combat_target = target

            aim_yaw, aim_pitch = self._lead_aim(target, pos)
            # Only shoot if in realistic combat range (< 50m) to avoid alerting distant bots
            should_fire = tdist < 50.0

            away_x, away_y = norm2d(px - target["pos"][0], py - target["pos"][1])
            tang_x, tang_y = rotate90(away_x, away_y, self.strafe_dir)

            if tdist < 18.0:
                move_x = away_x * 0.75 + tang_x * 0.25
                move_y = away_y * 0.75 + tang_y * 0.25
            elif tdist < 40.0:
                move_x = tang_x * 0.70 + to_zone_x * 0.20 + away_x * 0.10
                move_y = tang_y * 0.70 + to_zone_y * 0.20 + away_y * 0.10
            else:
                move_x = tang_x * 0.40 + to_zone_x * 0.60
                move_y = tang_y * 0.40 + to_zone_y * 0.60

        # CASE C: SUPPLY DROP RECOVERY (Uncontested drops only)
        elif drops:
            best_drop = None
            for d in drops:
                lands_in = d.get("lands_in", 999.0)
                holder = d.get("holder")
                ddist = dist2d(d["pos"], pos)
                if lands_in <= 10.0 and (holder is None or holder == me.get("id")) and ddist < 75.0:
                    best_drop = d
                    break

            if best_drop and not enemy_mechs:
                dp = best_drop["pos"]
                dr = best_drop.get("radius", 8.0)
                ddist = dist2d(dp, pos)
                if ddist < dr * 0.65:
                    # Orbit gently inside the capture zone (never stand dead still)
                    dx_d, dy_d = norm2d(dp[0] - px, dp[1] - py)
                    dtx, dty = rotate90(dx_d, dy_d, self.strafe_dir)
                    move_x = dtx * 0.6 + dx_d * 0.2
                    move_y = dty * 0.6 + dy_d * 0.2
                else:
                    move_x, move_y = norm2d(dp[0] - px, dp[1] - py)
                aim_yaw = math.atan2(move_y, move_x)

        # CASE D: EARLY NPC FARMING (+4% HP / +4% DMG Cubes)
        elif npcs and match_time < 140.0 and cubes < 6 and not enemy_mechs:
            npc = npcs[0]
            ndist = npc["_dist"]
            if ndist < 45.0:
                combat_target = npc
                aim_yaw, aim_pitch = self._lead_aim(npc, pos)
                should_fire = True
                nx, ny = norm2d(px - npc["pos"][0], py - npc["pos"][1])
                ntx, nty = rotate90(nx, ny, self.strafe_dir)
                if ndist < 15.0:
                    move_x, move_y = nx * 0.6 + ntx * 0.4, ny * 0.6 + nty * 0.4
                else:
                    move_x, move_y = -nx * 0.5 + ntx * 0.5, -ny * 0.5 + nty * 0.5

        # CASE E: SAFE-ZONE PERIMETER ORBIT (The Hiding / Stealth Strategy)
        else:
            # Stay at 65-80% safe zone radius (outer ring) away from central crossfire
            if dist_to_next < (zr * 0.35):
                out_x, out_y = norm2d(px - zc[0], py - zc[1])
                tx, ty = rotate90(out_x, out_y, self.strafe_dir)
                move_x = out_x * 0.6 + tx * 0.4
                move_y = out_y * 0.6 + ty * 0.4
            elif dist_to_next > (zr * 0.85):
                move_x, move_y = to_zone_x, to_zone_y
            else:
                to_c_x, to_c_y = norm2d(zc[0] - px, zc[1] - py)
                tx, ty = rotate90(to_c_x, to_c_y, self.strafe_dir)
                move_x = tx * 0.80 + to_c_x * 0.20
                move_y = ty * 0.80 + to_c_y * 0.20

            # Turn toward near sounds for situational awareness
            if sounds:
                for s in sounds:
                    if s.get("dist") == "near" and s.get("kind") in ("gun", "laser", "lightning", "mortar"):
                        aim_yaw = s.get("bearing", aim_yaw)
                        break

        # ─── 8. Dodge Integration & Zone Urgency ───────────────────────
        if dodge_urgency > 0.08:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        if outside_curr:
            move_x = to_zone_x * 0.85 + move_x * 0.15
            move_y = to_zone_y * 0.85 + move_y * 0.15

        # ─── 9. Anti-Stuck & Curb Clearance ────────────────────────────
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if moved < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        self.last_pos = list(pos)

        if self.stuck_ticks > 1:
            move_x, move_y = rotate90(move_x, move_y, self.strafe_dir)
            if self.stuck_ticks > 5:
                self.strafe_dir *= -1
                move_x, move_y = -move_x, -move_y

        move_x, move_y = norm2d(move_x, move_y)

        # Jump every 25 ticks to clear 1.2m curbs + on stuck
        should_jump = ((current_tick % 25 == 0) or (self.stuck_ticks > 1)) and grounded

        # Boost logic
        should_boost = False
        if not in_zone or outside_curr:
            should_boost = True
        elif should_flee and energy > 25.0:
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 20.0:
            should_boost = True
        elif energy > 80.0 and dist_to_next > (zr * 0.70):
            should_boost = True

        # ─── 10. Precision Weapon Control (Laser Cycle Mastery) ────────
        fire_slot0 = False
        fire_slot1 = False

        if should_fire and combat_target is not None:
            tdist = combat_target.get("_dist", 40.0)

            # Slot 0: Gun
            w0 = weapons[0] if len(weapons) > 0 else None
            if w0 is not None:
                ammo0 = w0.get("ammo", 30)
                cd0 = w0.get("cooldown", 0.0)
                reload0 = w0.get("reload", 0.0)
                if cd0 <= 0.0 and ammo0 > 0 and reload0 <= 0.0:
                    fire_slot0 = True
                elif ammo0 <= 0 and reload0 <= 0.0:
                    action["reload"] = True

            # Slot 1: Secondary Weapon
            w1 = weapons[1] if len(weapons) > 1 else None
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                cd1 = w1.get("cooldown", 0.0)

                if w1_kind == "laser":
                    charge = w1.get("charge", 0.0)
                    if cd1 > 0.0:
                        fire_slot1 = False  # Release during cooldown so we walk at full speed!
                    elif charge >= self.laser_charge_time:
                        fire_slot1 = False  # Release trigger to fire 315-dmg instant beam!
                    else:
                        fire_slot1 = True   # Hold to charge
                elif w1_kind == "lightning" and tdist < 45.0:
                    fire_slot1 = (cd1 <= 0.0)
                elif w1_kind == "mortar" and 25.0 < tdist < 80.0:
                    fire_slot1 = (cd1 <= 0.0)
                elif w1_kind == "gun":
                    fire_slot1 = (cd1 <= 0.0)

        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )

        return action

    def _lead_aim(self, target: dict, my_pos: list) -> tuple:
        """2-iteration predictive lead aiming at torso center of mass."""
        tpos = target["pos"]
        tvel = target.get("vel", [0, 0, 0])
        px, py, pz = my_pos

        lead_t = 0.0
        for _ in range(2):
            lx = tpos[0] + tvel[0] * lead_t
            ly = tpy + tvy * lead_t
            d = dist2d([lx, ly], [px, py])
            lead_t = min(0.55, d / max(1.0, self.gun_speed))

        final_x = tpos[0] + tvel[0] * lead_t
        final_y = tpy + tvy * lead_t
        final_z = tpos[2] + tvel[2] * lead_t + 4.5  # torso center-of-mass

        yaw = math.atan2(final_y - py, final_x - px)
        hdist = max(1.0, dist2d([final_x, final_y], [px, py]))
        pitch = math.atan2(final_z - (pz + 7.0), hdist)
        return yaw, pitch


if __name__ == "__main__":
    mf.run(PhantomV2)
