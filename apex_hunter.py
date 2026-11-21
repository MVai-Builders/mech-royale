"""MVai Apex Hunter Bot v6 - Builders Buildathon Mech Royale
=============================================================
Strategy: Survival-first tactical kiter & apex predator
- Placement-focused survival: avoids early multi-bot crossfires
- #1 Projectile dodging: perpendicular evasion from incoming fire
- Sound-based threat awareness: faces and detects threats from rear
- True supply drop control: captures landed uncontested drops (3 cubes + full HP)
- Early-game NPC farming: rapid cube acquisition (+4% HP/DMG per cube)
- Dynamic circle-strafing with damage-reactive direction flipping
- Predictive 2-pass lead aim for 252 m/s projectile ballistics
- Smart weapon management: gun ammo/reload tracking & secondary burst
- Low HP tactical retreat to zone interior
- Anti-stuck curb jumping (clears 1.2m terrain steps)
"""

import math
import sys
import mechfight as mf


def dist2d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm2d(dx, dy):
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0, 0.0
    return dx / d, dy / d


def rotate90(dx, dy, direction=1):
    return -dy * direction, dx * direction


class ApexHunter:
    name = "MVai-Apex"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.reach = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.gun_speed = cfg.get("weapons", {}).get("gun", {}).get("speed", 252.0)
        self.slot = hello.get("slot", 0)

        # State tracking
        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1
        self.last_hp = 1000.0
        self.match_tick = 0
        self.ticks_since_damage = 999

    def act(self, obs: mf.Observation) -> mf.Action:
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

        action: mf.Action = {"tick": current_tick}

        # ─── 0. Event Handling & Threat Tracking ───────────────────
        hp_lost = self.last_hp - hp
        if hp_lost > 1.0:
            self.strafe_dir *= -1
            self.ticks_since_damage = 0
        else:
            self.ticks_since_damage += 1
        self.last_hp = hp
        hp_ratio = hp / max(1.0, max_hp)

        # ─── 1. Upgrade Selection (Armor > Sensor > HP/Repair > Speed > DPS) ─
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority = {
                "armor": 12, "damage_taken": 12,
                "sensor": 10, "vision": 10,
                "repair": 9, "hp": 9, "health": 9, "regen": 9,
                "speed": 7, "boost": 6, "energy": 6,
                "rate": 5, "damage": 4, "range": 3, "reload": 2
            }
            pick_idx, best_val = 0, -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                val = max((v for k, v in priority.items() if k in desc), default=3)
                if val > best_val:
                    best_val, pick_idx = val, idx
            action["pick"] = pick_idx

        # ─── 2. Weapon Looting (Pickups on Ground) ──────────────────
        all_pickups = obs.get("pickups", [])
        near = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None
        if not has_secondary and near:
            pref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}
            best_pickup = max(near, key=lambda p: pref.get(p.get("kind", ""), 0))
            if pref.get(best_pickup.get("kind", ""), 0) > 0:
                action["take"] = best_pickup["id"]

        # ─── 3. Zone Navigation Vectors ────────────────────────────
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)

        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)

        outside_curr = dist_to_curr > curr_radius
        outside_next = dist_to_next > zr

        # ─── 4. Entities & Perceptions ─────────────────────────────
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

        # ─── 5. Incoming Projectile Dodging (#1 Survival Factor) ───
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
            pdist = math.hypot(dx, dy)
            t_impact = pdist / pspeed

            if 0.0 < t_impact < 0.65:
                # Predicted position of bullet when it reaches our distance
                pred_bx = pp[0] + pv[0] * t_impact
                pred_by = pp[1] + pv[1] * t_impact
                miss_d = dist2d([pred_bx, pred_by], pos)

                if miss_d < 8.0:
                    # Move perpendicular to bullet trajectory
                    b_dir_x, b_dir_y = norm2d(pv[0], pv[1])
                    perp_x, perp_y = rotate90(b_dir_x, b_dir_y, self.strafe_dir)
                    urgency = (1.0 - t_impact / 0.65) * (1.0 - miss_d / 8.0)
                    dodge_x += perp_x * urgency
                    dodge_y += perp_y * urgency
                    dodge_urgency = max(dodge_urgency, urgency)

        # ─── 6. Tactical Decision & Movement ───────────────────────
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None

        # Supply drop evaluation (from obs["drops"])
        best_drop = None
        for d in drops:
            ddist = dist2d(d["pos"], pos)
            # Landed, uncontested or held by us, reasonably close
            if d.get("lands_in", 0) <= 1.0 and (d.get("holder") is None or d.get("holder") == me.get("id")):
                if ddist < 75.0:
                    best_drop = d
                    break

        # Engagement logic:
        # Avoid fights when low HP or multiple close enemies
        should_flee = (hp_ratio < 0.3) or (len([e for e in enemy_mechs if e["_dist"] < 30.0]) >= 2)

        if should_flee and enemy_mechs:
            # Flee from closest enemy while staying in zone
            threat = enemy_mechs[0]
            away_x, away_y = norm2d(px - threat["pos"][0], py - threat["pos"][1])
            move_x = away_x * 0.6 + to_zone_x * 0.4
            move_y = away_y * 0.6 + to_zone_y * 0.4
            aim_yaw, aim_pitch = self._compute_lead_aim(threat["pos"], threat.get("vel", [0, 0, 0]), pos)
            if threat["_dist"] < 40.0:
                should_fire = True
                combat_target = threat

        elif enemy_mechs:
            target = enemy_mechs[0]
            combat_target = target
            tdist = target["_dist"]
            tpos = target["pos"]
            tvel = target.get("vel", [0, 0, 0])

            aim_yaw, aim_pitch = self._compute_lead_aim(tpos, tvel, pos)
            should_fire = True

            # Dynamic circle-strafing:
            ex, ey = norm2d(px - tpos[0], py - tpos[1])
            tx, ty = rotate90(ex, ey, self.strafe_dir)

            if tdist < 18.0:
                # Push back strongly while strafing
                move_x = ex * 0.75 + tx * 0.25
                move_y = ey * 0.75 + ty * 0.25
            elif tdist < 45.0:
                # Optimal combat range: 70% strafe, 30% zone inward
                move_x = tx * 0.70 + to_zone_x * 0.30
                move_y = ty * 0.70 + to_zone_y * 0.30
            else:
                # Approach smoothly while strafing
                move_x = -ex * 0.45 + tx * 0.35 + to_zone_x * 0.20
                move_y = -ey * 0.45 + ty * 0.35 + to_zone_y * 0.20

        elif best_drop:
            # Secure supply drop (3 cubes + full HP)
            dp = best_drop["pos"]
            move_x, move_y = norm2d(dp[0] - px, dp[1] - py)
            aim_yaw = math.atan2(move_y, move_x)

        elif npcs and (match_time < 180.0 or cubes < 6) and npcs[0]["_dist"] < 50.0:
            # Farm NPC early for cubes (2 cubes each = +8% HP & dmg)
            npc = npcs[0]
            ndist = npc["_dist"]
            aim_yaw, aim_pitch = self._compute_lead_aim(npc["pos"], npc.get("vel", [0, 0, 0]), pos)
            should_fire = True
            combat_target = npc

            nx, ny = norm2d(px - npc["pos"][0], py - npc["pos"][1])
            ntx, nty = rotate90(nx, ny, self.strafe_dir)
            if ndist < 15.0:
                move_x = nx * 0.6 + ntx * 0.4
                move_y = ny * 0.6 + nty * 0.4
            else:
                move_x = -nx * 0.5 + ntx * 0.5
                move_y = -ny * 0.5 + nty * 0.5

        elif crates and not enemy_mechs and cubes < 4:
            # Early crate smashing for loot & cubes
            near_crates = [c for c in crates if dist2d(c["pos"], pos) < 35.0]
            if near_crates:
                c = near_crates[0]
                cp = c["pos"]
                aim_yaw = math.atan2(cp[1] - py, cp[0] - px)
                aim_pitch = math.atan2(cp[2] + 2.0 - (pz + 7.0), max(1.0, dist2d(cp, pos)))
                should_fire = True
                move_x, move_y = norm2d(cp[0] - px, cp[1] - py)

        else:
            # Perimeter survival patrolling: stay around 65-75% of zone radius
            # Avoid center meat-grinder where house bots concentrate
            if dist_to_next < zr * 0.35:
                out_x, out_y = norm2d(px - zc[0], py - zc[1])
                tx, ty = rotate90(out_x, out_y, self.strafe_dir)
                move_x = out_x * 0.6 + tx * 0.4
                move_y = out_y * 0.6 + ty * 0.4
            elif dist_to_next > zr * 0.85:
                move_x, move_y = to_zone_x, to_zone_y
            else:
                tx, ty = rotate90(to_zone_x, to_zone_y, self.strafe_dir)
                move_x = tx * 0.75 + to_zone_x * 0.25
                move_y = ty * 0.75 + to_zone_y * 0.25

            # Sound awareness when no visible enemies: look towards nearby sounds
            if sounds:
                for s in sounds:
                    if s.get("dist") in ("near", "mid") and s.get("kind") in ("gun", "mortar", "laser", "lightning"):
                        aim_yaw = s.get("bearing", aim_yaw)
                        break

        # ─── 7. Blend Dodging Vectors ──────────────────────────────
        if dodge_urgency > 0.05:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        # ─── 8. Anti-Stuck & Jump Evasion ───────────────────────────
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

        # Normalize final movement
        move_x, move_y = norm2d(move_x, move_y)

        # Jump: every 25 ticks to clear terrain curbs (1.2m) + when stuck
        should_jump = ((current_tick % 25 == 0) or (self.stuck_ticks > 1)) and grounded

        # Boost: out of zone, dodging, or fleeing
        should_boost = (not in_zone) or (outside_curr) or (dodge_urgency > 0.4 and energy > 25.0) or (should_flee and energy > 30.0) or (energy > 75.0 and dist_to_next > zr * 0.6)

        # ─── 9. Smart Weapon Fire Control ──────────────────────────
        fire_slot0 = False
        fire_slot1 = False
        if should_fire:
            # Slot 0: Arm Gun
            w0 = weapons[0] if len(weapons) > 0 else None
            if w0 is not None:
                ammo = w0.get("ammo", 30)
                cd = w0.get("cooldown", 0.0)
                reloading = w0.get("reload", 0.0)
                if cd <= 0.0 and ammo > 0 and reloading <= 0.0:
                    fire_slot0 = True
                elif ammo <= 0 and reloading <= 0.0:
                    action["reload"] = True

            # Slot 1: Secondary Weapon
            w1 = weapons[1] if len(weapons) > 1 else None
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                cd1 = w1.get("cooldown", 0.0)
                tdist = combat_target.get("_dist", 50.0) if combat_target else 50.0
                if w1_kind == "laser":
                    fire_slot1 = True
                elif w1_kind == "lightning" and tdist < 45.0:
                    fire_slot1 = cd1 <= 0.0
                elif w1_kind == "mortar" and 25.0 < tdist < 90.0:
                    fire_slot1 = cd1 <= 0.0
                elif w1_kind == "gun":
                    fire_slot1 = cd1 <= 0.0

        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )
        return action

    def _compute_lead_aim(self, target_pos, target_vel, my_pos):
        """2-pass iterative lead prediction for ballistic targeting."""
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
        final_z = tpz + tvz * lead_t + 4.5  # Mech torso center-of-mass

        yaw = math.atan2(final_y - mpy, final_x - mpx)
        hdist = max(1.0, dist2d((final_x, final_y), (mpx, mpy)))
        pitch = math.atan2(final_z - (mpz + 7.0), hdist)
        return yaw, pitch


if __name__ == "__main__":
    mf.run(ApexHunter)
