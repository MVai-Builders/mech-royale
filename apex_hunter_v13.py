"""MVai Apex v13 — Maximum Placement Engine
=============================================
Built on the PROVEN v10 foundation (2.92 base) with high-impact fixes:

CRITICAL INSIGHT: House bots share vision, never fight each other, and hunt you
weakest-first. Pure hiding fails (v8=0.69). You MUST fight + survive.
Score = mean(16 - placement). Average ~9th = 7.0 score.

KEY IMPROVEMENTS OVER v10:
1. MORTAR SUPPORT: Pick up mortar (200 direct + 160 splash, knockback escape).
   Proper ballistic pitch calculation for arced fire.
2. FIXED LASER: v10's proven always-hold approach (auto-fires at full charge).
   v12's release logic was WRONG and caused missed shots.
3. RESTORED UPGRADE PRIORITIES: Armor > Sensor/Vision > Sustain > Speed > DPS.
   Sensor upgrades (+15m prox, +25m vision) are game-changing for positioning.
4. KILL CHASING: Boost-chase wounded enemies (<25% HP) for 4-cube kill reward.
   Each kill = +16% HP and +16% DMG from cubes alone.
5. THIRD-PARTY TIMING: Attack wounded enemies even during multi-enemy encounters.
   Focus fire on the weakest to secure kills fast.
6. ZONE AS WEAPON: Kite enemies toward zone edge. They take zone DPS while you don't.
7. REGEN EXPLOITATION: After 5s without combat, 45 HP/s regen kicks in.
   Disengage at <40% HP to heal, re-engage at >70%.
8. BETTER FLEE: Only flee from truly overwhelming situations (3+ close enemies,
   not 2 as in v10). v8 proved over-fleeing kills your score.
9. ENERGY DISCIPLINE: Never boost below 15 energy except zone escape.
   Save energy for critical dodge/flee moments.
10. AGGRESSIVE NPC FARMING: No health gate on NPC farming (v12 bug fixed).
    Farm NPCs freely for 2 cubes each = fast power scaling.
"""

import math
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
        self.mortar_speed = cfg.get("weapons", {}).get("mortar", {}).get("speed", 72.0)
        self.mortar_gravity = cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22.0)
        self.lightning_range = cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0)
        self.lightning_cone_deg = cfg.get("weapons", {}).get("lightning", {}).get("cone_deg", 30.0)
        self.slot = hello.get("slot", 0)
        self.my_id = hello.get("slot", 0)
        self.mech_cfg = cfg.get("mech", {})

        # State tracking
        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1
        self.last_hp = 1400.0
        self.aim_bad = 0
        self.target_id = None
        self.last_damaged_bearing = None
        self.ticks_since_damage = 100
        self.ticks_since_dealt = 100
        self.rng = hello.get("seed", 42) & 0x7FFFFFFF

    def _rand(self):
        self.rng = (self.rng * 1103515245 + 12345) & 0x7FFFFFFF
        return self.rng / 0x7FFFFFFF

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        hp = me.get("hp", 1400.0)
        max_hp = me.get("max_hp", 1400.0)
        energy = me.get("energy", 100.0)
        in_zone = me.get("in_zone", True)
        grounded = me.get("grounded", True)
        cubes = me.get("cubes", 0)
        weapons = me.get("weapons", [None, None])
        current_tick = obs["tick"]
        match_time = obs.get("t", 0.0)
        alive = obs.get("alive", 16)
        my_aim = me.get("aim", [0.0, 0.0])
        my_stats = me.get("stats", {})
        muzzle_h = my_stats.get("muzzle_height", 7.0)
        eye_h = my_stats.get("eye_height", 8.0)

        action: mf.Action = {"tick": current_tick}
        hp_ratio = hp / max(1.0, max_hp)

        # ═══════════════════════════════════════════════════════════
        # 0. EVENT PROCESSING & DAMAGE TRACKING
        # ═══════════════════════════════════════════════════════════
        hp_lost = self.last_hp - hp
        self.last_damaged_bearing = None
        rear_hit = False

        for ev in obs.get("events", []):
            et = ev.get("type")
            if et == "damaged":
                self.ticks_since_damage = 0
                b = ev.get("bearing")
                if b is not None:
                    self.last_damaged_bearing = float(b)
                    if ev.get("rear"):
                        rear_hit = True
            elif et == "hit" or et == "kill":
                self.ticks_since_dealt = 0

        if hp_lost > 2.0:
            self.strafe_dir *= -1
            self.ticks_since_damage = 0
        else:
            self.ticks_since_damage += 1
        self.ticks_since_dealt += 1
        self.last_hp = hp

        # ═══════════════════════════════════════════════════════════
        # 1. UPGRADE SELECTION: Armor > Sensor/Vision > Sustain > Speed > DPS
        # ═══════════════════════════════════════════════════════════
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority = {
                "armor": 16, "damage_taken": 16,
                "sensor": 13, "vision": 13,
                "repair": 11, "hp": 11, "health": 11, "regen": 10,
                "speed": 9, "boost": 8, "energy": 8,
                "rate": 6, "damage": 5, "cooldown": 6,
                "range": 4, "reload": 3, "spread": 3, "charge": 4
            }
            pick_idx, best_val = 0, -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                val = max((v for k, v in priority.items() if k in desc), default=3)
                if val > best_val:
                    best_val, pick_idx = val, idx
            action["pick"] = pick_idx

        # ═══════════════════════════════════════════════════════════
        # 2. WEAPON LOOTING: Laser > Mortar > Lightning (ALL weapons useful)
        # ═══════════════════════════════════════════════════════════
        all_pickups = obs.get("pickups", [])
        near = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None
        secondary_kind = weapons[1].get("kind") if has_secondary else None

        weapon_rank = {"laser": 5, "mortar": 4, "lightning": 3, "gun": 0}

        if near:
            best_pickup = max(near, key=lambda p: weapon_rank.get(p.get("kind", ""), 0))
            bp_kind = best_pickup.get("kind", "")
            bp_val = weapon_rank.get(bp_kind, 0)

            if not has_secondary and bp_val > 0:
                action["take"] = best_pickup["id"]
            elif has_secondary and bp_val > weapon_rank.get(secondary_kind, 0) + 1:
                # Swap for a significantly better weapon
                action["take"] = best_pickup["id"]
                action["drop"] = 1

        # ═══════════════════════════════════════════════════════════
        # 3. ZONE GEOMETRY & URGENCY
        # ═══════════════════════════════════════════════════════════
        zone = obs.get("zone", {})
        zc_curr = zone.get("center", [0.0, 0.0])
        zr_curr = zone.get("radius", 300.0)
        zc_next = zone.get("next_center", zc_curr)
        zr_next = zone.get("next_radius", zr_curr)
        shrink_start = zone.get("shrink_start", 999.0)
        shrink_end = zone.get("shrink_end", 999.0)
        zone_dps = zone.get("dps", 20.0)

        dist_to_next = dist2d(pos, zc_next)
        dist_to_curr = dist2d(pos, zc_curr)
        to_zone_x, to_zone_y = norm2d(zc_next[0] - px, zc_next[1] - py)
        outside_curr = dist_to_curr > (zr_curr - 5.0)

        # Zone urgency: how soon do we need to be inside next zone?
        walk_speed = my_stats.get("walk_speed", 11.7)
        dist_outside_next = max(0.0, dist_to_next - zr_next + 10.0)
        time_to_reach = dist_outside_next / max(1.0, walk_speed)
        time_until_shrink = max(0.0, shrink_start - match_time)
        zone_urgent = outside_curr or (time_to_reach > 0 and time_until_shrink > 0 and time_to_reach / max(0.1, time_until_shrink) > 0.6)

        # ═══════════════════════════════════════════════════════════
        # 4. ENTITY ANALYSIS & TARGET SELECTION
        # ═══════════════════════════════════════════════════════════
        all_mechs = obs.get("mechs", [])
        enemy_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        for e in enemy_mechs:
            e["_dist"] = dist2d(e["pos"], pos)

        # Target selection: prioritize wounded targets at close range, stick to current target
        def target_rank(m):
            score = 0.0
            d = m["_dist"]
            ehp = m.get("hp", 1400.0)
            emax = m.get("max_hp", 1400.0)
            ehp_ratio = ehp / max(1.0, emax)

            # Distance scoring (prefer closer targets)
            if d < 20.0:
                score += 300.0 - d * 10.0
            elif d < 45.0:
                score += 200.0 - d * 2.0
            else:
                score += 100.0 - d

            # KILL CHASE: Heavily prioritize low-HP targets for cube income
            if ehp < 200.0:
                score += 400.0
            elif ehp < 400.0:
                score += 250.0
            elif ehp_ratio < 0.4:
                score += 150.0

            # Sticky targeting: moderate bonus to avoid target flapping
            if self.target_id is not None and m.get("id") == self.target_id:
                score += 120.0

            # Penalize heavily-armed targets at range
            e_weapons = m.get("weapons", [])
            if "laser" in e_weapons and d > 40.0:
                score -= 50.0

            return score

        enemy_mechs.sort(key=target_rank, reverse=True)
        if enemy_mechs:
            self.target_id = enemy_mechs[0].get("id")
        else:
            self.target_id = None

        npcs = [m for m in all_mechs if m.get("kind") == "npc"]
        for n in npcs:
            n["_dist"] = dist2d(n["pos"], pos)
        npcs.sort(key=lambda n: n["_dist"])

        projectiles = obs.get("projectiles", [])
        sounds = obs.get("sounds", [])
        drops = obs.get("drops", [])
        cubes_on_ground = obs.get("cubes", [])
        crates = obs.get("crates", [])

        enemies_close = [e for e in enemy_mechs if e["_dist"] < 30.0]
        enemies_near = [e for e in enemy_mechs if e["_dist"] < 45.0]

        # ═══════════════════════════════════════════════════════════
        # 5. PROJECTILE DODGING (filter own bullets)
        # ═══════════════════════════════════════════════════════════
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0

        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            # Filter: only dodge projectiles heading TOWARD us
            dot = pv[0] * (px - pp[0]) + pv[1] * (py - pp[1])
            if dot <= 0.0:
                continue

            pspeed = math.hypot(pv[0], pv[1], pv[2])
            if pspeed < 1.0:
                continue

            dx, dy = px - pp[0], py - pp[1]
            pdist = math.hypot(dx, dy)

            # Closest approach calculation
            vdx, vdy = pv[0], pv[1]
            denom = vdx * vdx + vdy * vdy
            if denom < 1.0:
                continue
            t_closest = max(0.0, (dx * vdx + dy * vdy) / denom)

            if t_closest < 0.7:
                cx = pp[0] + vdx * t_closest - px
                cy = pp[1] + vdy * t_closest - py
                miss_d = math.hypot(cx, cy)

                threat_r = 9.0
                if proj.get("kind") == "mortar":
                    threat_r = 14.0  # Mortar has splash radius

                if miss_d < threat_r:
                    b_dir_x, b_dir_y = norm2d(vdx, vdy)
                    perp_x, perp_y = rotate90(b_dir_x, b_dir_y, self.strafe_dir)
                    urgency = (1.0 - t_closest / 0.7) * max(0.0, 1.0 - miss_d / threat_r)
                    if proj.get("kind") == "mortar":
                        urgency *= 1.6
                    dodge_x += perp_x * urgency
                    dodge_y += perp_y * urgency
                    dodge_urgency = max(dodge_urgency, urgency)

        # ═══════════════════════════════════════════════════════════
        # 6. SUPPLY DROP EVALUATION
        # ═══════════════════════════════════════════════════════════
        best_drop = None
        for d in drops:
            ddist = dist2d(d["pos"], pos)
            lands_in = d.get("lands_in", 999.0)
            holder = d.get("holder")
            # Time budget: how early should we head to the drop?
            lead_t = max(12.0, ddist / 15.0 + 2.0)
            # Skip drops outside current zone
            if dist2d(d["pos"], zc_curr) > zr_curr + 10.0:
                continue
            if lands_in <= lead_t and (holder is None or holder == self.my_id):
                if ddist < 150.0:
                    best_drop = d
                    break

        # ═══════════════════════════════════════════════════════════
        # 7. TACTICAL MOVEMENT & COMBAT DECISIONS
        # ═══════════════════════════════════════════════════════════
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None
        in_direct_combat = False
        is_fleeing = False

        # --- Kiting anchor: default toward zone, modified by objectives ---
        anchor_x, anchor_y, anchor_w = to_zone_x, to_zone_y, 0.20

        # WEAPON SEEK: kite toward laser/mortar/lightning when slot 1 is empty
        if not has_secondary and hp_ratio > 0.4 and all_pickups:
            wf = [p for p in all_pickups
                  if weapon_rank.get(p.get("kind", ""), 0) > 0
                  and dist2d(p["pos"], pos) < 50.0]
            if wf:
                wfp = min(wf, key=lambda p: dist2d(p["pos"], pos))
                # Only seek weapon if no enemies very close, or only 1 distant enemy
                if not enemies_close and (not enemy_mechs or (len(enemy_mechs) == 1 and enemy_mechs[0]["_dist"] > 20.0)):
                    anchor_x, anchor_y = norm2d(wfp["pos"][0] - px, wfp["pos"][1] - py)
                    anchor_w = 0.50

        # CUBE VACUUM: pull toward nearby cubes during combat
        if cubes_on_ground and hp_ratio > 0.3:
            close_c = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 22.0]
            if close_c:
                nearest_c = min(close_c, key=lambda c: dist2d(c["pos"], pos))
                cdist = dist2d(nearest_c["pos"], pos)
                c_dx, c_dy = norm2d(nearest_c["pos"][0] - px, nearest_c["pos"][1] - py)
                c_w = 0.65 if cdist < 8.0 else 0.40
                anchor_x = anchor_x * (1.0 - c_w) + c_dx * c_w
                anchor_y = anchor_y * (1.0 - c_w) + c_dy * c_w
                anchor_w = max(anchor_w, 0.40)

        # DROP CAPTURE: HP-budgeted capture logic
        cap_active = False
        cap_r, dd_c = 8.0, 0.0
        if best_drop:
            cap_r = best_drop.get("radius", 8.0)
            cap_s = best_drop.get("capture_s", 4.0)
            dpos = best_drop["pos"]
            dd_c = dist2d(dpos, pos)
            progress = best_drop.get("progress", 0.0)
            remaining = max(0.0, cap_s - progress)
            n_shoot = len(enemies_near)
            inside_enemies = [e for e in enemy_mechs if dist2d(e["pos"], dpos) < cap_r]
            exposure = remaining + max(0.0, (dd_c - cap_r * 0.5) / 15.0)
            hp_need = 110.0 * n_shoot * exposure
            if not inside_enemies and (n_shoot == 0 or hp - 60.0 > hp_need or (hp_ratio < 0.35 and dd_c < 50.0)):
                cap_active = True

        # --- Check for non-combat objectives ---
        target_weapon_pickup = None
        if not has_secondary and all_pickups and not enemy_mechs:
            good_weapons = [p for p in all_pickups
                            if weapon_rank.get(p.get("kind", ""), 0) > 0
                            and dist2d(p["pos"], pos) < 100.0
                            and dist2d(p["pos"], zc_next) < zr_next + 20.0]
            if good_weapons:
                target_weapon_pickup = max(good_weapons, key=lambda p: weapon_rank.get(p.get("kind",""), 0) - dist2d(p["pos"], pos) * 0.01)

        near_cube = None
        if cubes_on_ground and not enemy_mechs:
            close_cubes = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 40.0]
            if close_cubes:
                near_cube = min(close_cubes, key=lambda c: dist2d(c["pos"], pos))

        # ══════════════════════════════════════════════════════════
        # COMBAT BRANCH: Enemies visible
        # ══════════════════════════════════════════════════════════
        if enemy_mechs:
            in_direct_combat = True
            primary = enemy_mechs[0]
            combat_target = primary
            tpos = primary["pos"]
            tvel = primary.get("vel", [0, 0, 0])
            tdist = primary["_dist"]

            # Compute aim with lead prediction (v10 proven formula)
            aim_yaw, aim_pitch = self._compute_lead_aim(tpos, tvel, pos, pz + muzzle_h)
            should_fire = True

            # === REGEN EXPLOITATION ===
            # If we're healing (5s without combat) and HP < 70%, hold fire on distant targets
            regen_active = self.ticks_since_damage > 50 and self.ticks_since_dealt > 50
            if regen_active and hp_ratio < 0.70 and tdist > 50.0:
                should_fire = False
                # Still aim at them for front-armor protection

            # === FLEE DECISION ===
            # Only flee from truly overwhelming odds (v8 proved over-fleeing = death)
            n_very_close = len([e for e in enemy_mechs if e["_dist"] < 20.0])
            n_close = len(enemies_close)

            if (n_close >= 3 and hp_ratio < 0.60) or (n_very_close >= 2 and hp_ratio < 0.35):
                is_fleeing = True

            # === MOVEMENT ===
            # Away vector from primary (or centroid if multi-threat)
            away_x, away_y = norm2d(px - tpos[0], py - tpos[1])

            if len(enemies_near) >= 2:
                # Multi-threat centroid repulsion
                cax, cay = 0.0, 0.0
                for e in enemies_near:
                    w_ = 1.0 / max(5.0, e["_dist"])
                    nx_, ny_ = norm2d(px - e["pos"][0], py - e["pos"][1])
                    cax += nx_ * w_
                    cay += ny_ * w_
                cax, cay = norm2d(cax, cay)
                if cax != 0.0 or cay != 0.0:
                    away_x, away_y = cax, cay

            tx, ty = rotate90(away_x, away_y, self.strafe_dir)

            if is_fleeing:
                # Flee toward zone, away from threats
                move_x = away_x * 0.50 + to_zone_x * 0.40 + tx * 0.10
                move_y = away_y * 0.50 + to_zone_y * 0.40 + ty * 0.10
            elif tdist < 18.0:
                # Too close: backpedal while strafing
                move_x = away_x * 0.70 + tx * 0.30
                move_y = away_y * 0.70 + ty * 0.30
            elif tdist <= 38.0:
                # Sweet-spot: circle-strafe with anchor pull
                tcoef = max(0.30, 0.90 - anchor_w)
                move_x = tx * tcoef + anchor_x * anchor_w + away_x * 0.05
                move_y = ty * tcoef + anchor_y * anchor_w + away_y * 0.05
            else:
                # Far: approach while strafing (but cautiously)
                # KILL CHASE: be more aggressive closing on wounded targets
                close_weight = 0.50
                ehp = primary.get("hp", 1400.0)
                if ehp < 300.0 and tdist < 70.0:
                    close_weight = 0.65  # Chase the kill for cubes!
                elif tdist > 80.0:
                    close_weight = 0.30  # Don't overcommit to distant targets

                tcoef = max(0.15, 0.55 - anchor_w)
                move_x = -away_x * close_weight + tx * tcoef + anchor_x * anchor_w
                move_y = -away_y * close_weight + ty * tcoef + anchor_y * anchor_w

            # === ZONE-AS-WEAPON ===
            # If enemy is near zone edge and we're safely inside, kite them toward the edge
            if in_zone and not outside_curr:
                for e in enemies_near[:1]:
                    e_dist_to_zone = dist2d(e["pos"], zc_curr)
                    if e_dist_to_zone > zr_curr * 0.85 and dist_to_curr < zr_curr * 0.70:
                        # Enemy is near zone edge — kite them further out
                        to_enemy_x, to_enemy_y = norm2d(e["pos"][0] - px, e["pos"][1] - py)
                        # Stay between zone center and enemy
                        kite_x = to_enemy_x * 0.3 + tx * 0.7
                        kite_y = to_enemy_y * 0.3 + ty * 0.7
                        move_x = kite_x
                        move_y = kite_y

            # === DROP CAPTURE ORBIT ===
            if cap_active:
                dpx, dpy = best_drop["pos"][0], best_drop["pos"][1]
                ddx, ddy = norm2d(dpx - px, dpy - py)
                if dd_c > cap_r * 0.75:
                    move_x = ddx * 0.85 + tx * 0.15
                    move_y = ddy * 0.85 + ty * 0.15
                else:
                    ox, oy = norm2d(px - dpx, py - dpy)
                    otx, oty = rotate90(ox, oy, self.strafe_dir)
                    pull = 0.0 if dd_c < cap_r * 0.4 else 0.4 * (dd_c - cap_r * 0.4) / max(0.1, cap_r * 0.35)
                    move_x = otx * 0.85 - ox * pull
                    move_y = oty * 0.85 - oy * pull

        # ══════════════════════════════════════════════════════════
        # NON-COMBAT BRANCHES (no enemies visible)
        # ══════════════════════════════════════════════════════════
        elif cap_active and best_drop:
            dp = best_drop["pos"]
            d_dist = dist2d(dp, pos)
            d_radius = best_drop.get("radius", 8.0)
            if d_dist > d_radius * 0.6:
                move_x, move_y = norm2d(dp[0] - px, dp[1] - py)
            else:
                ox, oy = norm2d(px - dp[0], py - dp[1])
                otx, oty = rotate90(ox, oy, self.strafe_dir)
                move_x, move_y = otx * 0.8, oty * 0.8
            aim_yaw = math.atan2(move_y, move_x)

        elif target_weapon_pickup:
            wp = target_weapon_pickup["pos"]
            move_x, move_y = norm2d(wp[0] - px, wp[1] - py)
            aim_yaw = math.atan2(move_y, move_x)

        elif near_cube:
            cp = near_cube["pos"]
            move_x, move_y = norm2d(cp[0] - px, cp[1] - py)
            aim_yaw = math.atan2(move_y, move_x)

        elif npcs and npcs[0]["_dist"] < 45.0 and (match_time < 200.0 or cubes < 8):
            # Farm NPC for 2 cubes — NO health gate (v12 bug fixed)
            npc = npcs[0]
            combat_target = npc
            aim_yaw, aim_pitch = self._compute_lead_aim(npc["pos"], npc.get("vel", [0, 0, 0]), pos, pz + muzzle_h)
            should_fire = True

            nx, ny = norm2d(px - npc["pos"][0], py - npc["pos"][1])
            ntx, nty = rotate90(nx, ny, self.strafe_dir)
            if npc["_dist"] < 15.0:
                move_x = nx * 0.55 + ntx * 0.45
                move_y = ny * 0.55 + nty * 0.45
            else:
                move_x = -nx * 0.50 + ntx * 0.50
                move_y = -ny * 0.50 + nty * 0.50

        elif crates and cubes < 6:
            close_crates = [c for c in crates if dist2d(c["pos"], pos) < 30.0 and dist2d(c["pos"], zc_next) < zr_next + 10.0]
            if close_crates:
                c = min(close_crates, key=lambda c: dist2d(c["pos"], pos))
                cp = c["pos"]
                aim_yaw = math.atan2(cp[1] - py, cp[0] - px)
                aim_pitch = math.atan2(cp[2] + 2.0 - (pz + muzzle_h), max(1.0, dist2d(cp, pos)))
                should_fire = True
                combat_target = c
                move_x, move_y = norm2d(cp[0] - px, cp[1] - py)
            else:
                self._idle_orbit(pos, zc_next, zr_next, dist_to_next, to_zone_x, to_zone_y, sounds, action)
                # Get aim from the idle orbit
                move_x = action.get("_mx", to_zone_x)
                move_y = action.get("_my", to_zone_y)
                aim_yaw = action.get("_ay", aim_yaw)
                action.pop("_mx", None)
                action.pop("_my", None)
                action.pop("_ay", None)
        else:
            self._idle_orbit(pos, zc_next, zr_next, dist_to_next, to_zone_x, to_zone_y, sounds, action)
            move_x = action.get("_mx", to_zone_x)
            move_y = action.get("_my", to_zone_y)
            aim_yaw = action.get("_ay", aim_yaw)
            action.pop("_mx", None)
            action.pop("_my", None)
            action.pop("_ay", None)

        # ═══════════════════════════════════════════════════════════
        # 8. DODGE INTEGRATION
        # ═══════════════════════════════════════════════════════════
        if dodge_urgency > 0.04:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.88, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        # ═══════════════════════════════════════════════════════════
        # 9. ZONE ESCAPE OVERRIDE
        # ═══════════════════════════════════════════════════════════
        if outside_curr:
            w = min(0.92, 0.75 + zone_dps / 300.0)
            move_x = to_zone_x * w + move_x * (1.0 - w)
            move_y = to_zone_y * w + move_y * (1.0 - w)

        # ═══════════════════════════════════════════════════════════
        # 10. ANTI-STUCK
        # ═══════════════════════════════════════════════════════════
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if moved < 0.13:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = max(0, self.stuck_ticks - 1)
        self.last_pos = list(pos)

        if self.stuck_ticks > 1:
            # Random angle deflection
            angle = (0.4 + self._rand() * 0.6) * math.pi * self.strafe_dir
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            nmx = move_x * cos_a - move_y * sin_a
            nmy = move_x * sin_a + move_y * cos_a
            move_x, move_y = nmx, nmy
            if self.stuck_ticks > 5:
                self.strafe_dir *= -1
                self.stuck_ticks = 0

        # Normalize
        move_x, move_y = norm2d(move_x, move_y)

        # ═══════════════════════════════════════════════════════════
        # 11. JUMP & BOOST
        # ═══════════════════════════════════════════════════════════
        should_jump = False
        should_boost = False

        # Jump: curb clearance + stuck escape
        if grounded and (self.stuck_ticks > 1 or current_tick % 28 == 0):
            should_jump = True

        # Boost priorities (energy-disciplined):
        if not in_zone or outside_curr:
            should_boost = True  # Zone escape: always boost
        elif is_fleeing and energy > 15.0:
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 15.0:
            should_boost = True
        elif in_direct_combat and enemies_close and enemies_close[0]["_dist"] < 14.0 and energy > 20.0:
            should_boost = True  # Escape rush
        elif cap_active and best_drop and dd_c > 20.0 and energy > 60.0:
            should_boost = True  # Rush to drop
        elif zone_urgent and energy > 50.0:
            should_boost = True  # Get to zone
        elif energy > 85.0 and dist_to_next > zr_next * 0.65:
            should_boost = True  # Excess energy, need to reposition

        # === KILL CHASE BOOST ===
        if in_direct_combat and combat_target and not is_fleeing:
            ehp = combat_target.get("hp", 1400.0)
            edist = combat_target.get("_dist", 999.0)
            if ehp < 250.0 and edist < 50.0 and edist > 20.0 and energy > 30.0 and hp_ratio > 0.4:
                should_boost = True  # Chase the kill!

        # ═══════════════════════════════════════════════════════════
        # 12. AIM CONVERGENCE GATE (Yaw-only, v10 proven)
        # ═══════════════════════════════════════════════════════════
        aim_ok = True
        actual_aim_yaw = my_aim[0] if my_aim else 0.0

        if should_fire and combat_target is not None:
            err = abs((aim_yaw - actual_aim_yaw + math.pi) % (2 * math.pi) - math.pi)
            target_dist = max(5.0, combat_target.get("_dist", 30.0))

            # Wider cone for mortar (splash), tighter for gun
            hit_r = 3.0
            if has_secondary and secondary_kind == "mortar":
                hit_r = 7.0
            max_err = math.atan2(hit_r, target_dist) + 0.06

            if err > max_err and target_dist > 14.0:
                self.aim_bad += 1
                aim_ok = (self.aim_bad > 6)  # Anti-lockup
            else:
                self.aim_bad = 0

        # ═══════════════════════════════════════════════════════════
        # 13. TACTICAL RELOAD
        # ═══════════════════════════════════════════════════════════
        w0 = weapons[0] if len(weapons) > 0 else None
        if not in_direct_combat or (enemy_mechs and enemy_mechs[0]["_dist"] > 45.0):
            if w0 is not None:
                ammo = w0.get("ammo", 12)
                reloading = w0.get("reload", 0.0)
                if ammo < 10 and reloading <= 0.0:
                    action["reload"] = True

        # ═══════════════════════════════════════════════════════════
        # 14. WEAPON FIRING
        # ═══════════════════════════════════════════════════════════
        fire_slot0 = False
        fire_slot1 = False

        if should_fire:
            # Slot 0: Arm Gun
            if w0 is not None:
                ammo = w0.get("ammo", 12)
                cd = w0.get("cooldown", 0.0)
                reloading = w0.get("reload", 0.0)
                if cd <= 0.0 and ammo > 0 and reloading <= 0.0 and aim_ok:
                    fire_slot0 = True
                elif ammo <= 0 and reloading <= 0.0:
                    action["reload"] = True

            # Slot 1: Secondary weapon
            w1 = weapons[1] if len(weapons) > 1 else None
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                cd1 = w1.get("cooldown", 0.0)
                tdist = combat_target.get("_dist", 50.0) if combat_target else 50.0

                if w1_kind == "laser":
                    # v10 PROVEN: Just hold trigger. It auto-fires at full charge.
                    # Releasing early = wasted damage. Holding past full = fires on its own.
                    if cd1 <= 0.0:
                        fire_slot1 = True

                elif w1_kind == "lightning":
                    # Lightning: 45m range, 30° cone, costs energy
                    if tdist < self.lightning_range and cd1 <= 0.0 and energy > 6.0:
                        aim_err = abs((aim_yaw - actual_aim_yaw + math.pi) % (2 * math.pi) - math.pi)
                        cone_rad = math.radians(self.lightning_cone_deg * 0.8)
                        if aim_err < cone_rad:
                            fire_slot1 = True

                elif w1_kind == "mortar":
                    # MORTAR: Compute ballistic arc pitch + mortar-speed lead
                    if cd1 <= 0.0 and 15.0 < tdist < 200.0 and aim_ok:
                        mortar_pitch = self._compute_mortar_pitch(tdist, combat_target, pos, pz)
                        if mortar_pitch is not None:
                            # Compute mortar-specific yaw lead (slower projectile needs more lead)
                            tvel_m = combat_target.get("vel", [0, 0, 0])
                            # Approximate flight time for low arc mortar
                            cos_p = math.cos(mortar_pitch) if mortar_pitch < 1.5 else 0.1
                            flight_t = tdist / max(1.0, self.mortar_speed * cos_p)
                            flight_t = min(3.0, flight_t)
                            # Lead position
                            tpos_m = combat_target.get("pos", pos)
                            lead_x = tpos_m[0] + tvel_m[0] * flight_t
                            lead_y = tpos_m[1] + tvel_m[1] * flight_t
                            mortar_yaw = math.atan2(lead_y - py, lead_x - px)
                            # Blend mortar aim with gun aim (mortar does 200+160 dmg vs gun's 45)
                            aim_yaw = mortar_yaw
                            aim_pitch = mortar_pitch
                            fire_slot1 = True

                elif w1_kind == "gun":
                    if cd1 <= 0.0 and aim_ok:
                        fire_slot1 = True

        # ═══════════════════════════════════════════════════════════
        # 15. REAR ARMOR DEFENSE
        # ═══════════════════════════════════════════════════════════
        # If hit from behind, snap aim toward attacker to present front armor
        if rear_hit and self.last_damaged_bearing is not None and not should_fire:
            aim_yaw = self.last_damaged_bearing

        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )
        return action

    def _idle_orbit(self, pos, zc, zr, dist_to_next, to_zone_x, to_zone_y, sounds, action):
        """Movement when no enemies visible: orbit zone perimeter, avoid combat sounds."""
        px, py = pos[0], pos[1]

        if dist_to_next < zr * 0.30:
            out_x, out_y = norm2d(px - zc[0], py - zc[1])
            tx, ty = rotate90(out_x, out_y, self.strafe_dir)
            mx = out_x * 0.55 + tx * 0.45
            my = out_y * 0.55 + ty * 0.45
        elif dist_to_next > zr * 0.80:
            mx, my = to_zone_x, to_zone_y
        else:
            tx, ty = rotate90(to_zone_x, to_zone_y, self.strafe_dir)
            mx = tx * 0.78 + to_zone_x * 0.22
            my = ty * 0.78 + to_zone_y * 0.22

        ay = math.atan2(my, mx)

        # Sound awareness: face toward combat sounds
        # But move AWAY from "near" combat to avoid unnecessary fights
        for s in sounds:
            if s.get("kind") in ("gun", "mortar", "laser", "lightning"):
                if s.get("dist") == "near":
                    # Near combat: move perpendicular (don't walk into it)
                    sb = s.get("bearing", 0.0)
                    # Face it for awareness
                    ay = sb
                    # Move perpendicular to it
                    perp_x, perp_y = rotate90(math.cos(sb), math.sin(sb), self.strafe_dir)
                    mx = mx * 0.4 + perp_x * 0.6
                    my = my * 0.4 + perp_y * 0.6
                    break
                elif s.get("dist") == "mid":
                    ay = s.get("bearing", ay)
                    break

        # Face toward attacker bearing if recently damaged
        if self.last_damaged_bearing is not None:
            ay = self.last_damaged_bearing

        action["_mx"] = mx
        action["_my"] = my
        action["_ay"] = ay

    def _compute_lead_aim(self, target_pos, target_vel, my_pos, muzzle_z):
        """v10 proven 2-pass iterative lead prediction."""
        tpx, tpy, tpz = target_pos
        tvx, tvy, tvz = target_vel
        mpx, mpy = my_pos[0], my_pos[1]

        lead_t = 0.0
        for _ in range(3):
            lx = tpx + tvx * lead_t
            ly = tpy + tvy * lead_t
            d = dist2d((lx, ly), (mpx, mpy))
            lead_t = min(0.55, d / max(1.0, self.gun_speed))

        final_x = tpx + tvx * lead_t
        final_y = tpy + tvy * lead_t
        final_z = tpz + tvz * lead_t + 4.5  # Center of mass

        yaw = math.atan2(final_y - mpy, final_x - mpx)
        hdist = max(1.0, dist2d((final_x, final_y), (mpx, mpy)))
        pitch = math.atan2(final_z - muzzle_z, hdist)
        return yaw, pitch

    def _compute_mortar_pitch(self, horizontal_dist, target, my_pos, my_z):
        """Ballistic arc pitch for mortar shell. LOW arc for visible targets (faster, more accurate).
        Low arc flight times: ~0.7s at 50m, ~1.4s at 100m, ~2.5s at 200m."""
        v = self.mortar_speed
        g = self.mortar_gravity
        d = horizontal_dist

        # Height difference
        tgt_z = 4.5  # Center of mass height
        if isinstance(target, dict) and "pos" in target:
            tgt_z = target["pos"][2] + 4.5
        mortar_tube_z = my_z + 9.5
        dz = tgt_z - mortar_tube_z

        # Check if in range: v^4 - g*(g*d^2 + 2*dz*v^2) >= 0
        discriminant = v**4 - g * (g * d * d + 2.0 * dz * v * v)
        if discriminant < 0:
            return None  # Out of range

        sqrt_disc = math.sqrt(discriminant)
        # LOW arc: faster travel, more accurate for visible targets
        # theta = atan2(v^2 - sqrt_disc, g*d)
        pitch = math.atan2(v * v - sqrt_disc, g * d)

        # Clamp: at least slightly upward (don't fire into the ground)
        pitch = max(0.02, min(1.35, pitch))
        return pitch


if __name__ == "__main__":
    mf.run(ApexHunter)
