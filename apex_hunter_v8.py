"""MVai Apex Predator v8 — Survival-First Rewrite
==================================================
Paradigm shift from v7: SURVIVE, don't hunt.
Placement > kills. A cockroach, not a predator.

Key changes from v7.4:
1. Flee-from-groups: 2+ enemies visible = silent retreat (no firing)
2. Edge positioning: orbit at 85-95% zone radius, not 65%
3. HP-based disengagement: flee at HP<50% or when outnumbered
4. Zero telemetry overhead: no stderr, no /tmp writes
5. Sound = repel: run AWAY from gunfire sounds
6. No periodic jumping: only jump when stuck
7. Farm only when very early + safe (no nearby sounds)
8. Upgraded priority: mobility > sensor for survival
9. Smarter engagement: only 1v1 when healthy + advantaged
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
        self.flee_ticks = 0          # how long we've been in flee mode
        self.last_combat_tick = -999  # last tick we saw enemies
        self.damage_no_vis = 0.0     # damage taken with no enemies visible

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

        # ─── 0. Damage Tracking ─────────────────────────────────────
        hp_lost = self.last_hp - hp
        if hp_lost > 2.0:
            self.strafe_dir *= -1
        self.last_hp = hp
        hp_ratio = hp / max(1.0, max_hp)

        # ─── 1. Upgrades: Mobility & Defense > Everything ───────────
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority = {
                "armor": 14, "damage_taken": 14,
                "speed": 12, "boost": 11, "energy": 11,
                "repair": 10, "hp": 10, "health": 10, "regen": 10,
                "sensor": 6, "vision": 6,
                "rate": 5, "damage": 4, "range": 3, "reload": 2
            }
            pick_idx, best_val = 0, -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                val = max((v for k, v in priority.items() if k in desc), default=3)
                if val > best_val:
                    best_val, pick_idx = val, idx
            action["pick"] = pick_idx

        # ─── 2. Weapon Looting (within reach) ───────────────────────
        all_pickups = obs.get("pickups", [])
        near = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None

        if not has_secondary and near:
            pref = {"laser": 4, "lightning": 3, "mortar": 1, "gun": 0}
            best_pickup = max(near, key=lambda p: pref.get(p.get("kind", ""), 0))
            if pref.get(best_pickup.get("kind", ""), 0) > 0:
                action["take"] = best_pickup["id"]

        # ─── 3. Zone Geometry ───────────────────────────────────────
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)

        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)

        outside_curr = dist_to_curr > (curr_radius * 0.96)

        # ─── 4. Entity Perception ──────────────────────────────────
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
        cubes_on_ground = obs.get("cubes", [])
        crates = obs.get("crates", [])

        # Track unseen damage
        if hp_lost > 2.0 and not enemy_mechs:
            self.damage_no_vis += hp_lost

        # ─── 5. Threat Assessment ──────────────────────────────────
        n_visible = len(enemy_mechs)
        n_close = len([e for e in enemy_mechs if e["_dist"] < 45.0])
        n_very_close = len([e for e in enemy_mechs if e["_dist"] < 25.0])

        # Nearby threat sounds (indicates enemies we can't see yet)
        threat_sounds = [s for s in sounds
                         if s.get("dist") in ("near", "mid")
                         and s.get("kind") in ("gun", "mortar", "laser", "lightning")]
        sound_threat = len(threat_sounds)

        # ─── 6. State Decision: FLEE vs ENGAGE vs SURVIVE ──────────
        # FLEE conditions (survival first)
        should_flee = (
            (n_close >= 2) or                           # outnumbered
            (n_very_close >= 1 and hp_ratio < 0.45) or  # low HP with enemy close
            (hp_ratio < 0.30) or                        # critically low HP
            (n_visible >= 3) or                         # heavily outnumbered
            (n_close >= 1 and hp_ratio < 0.55 and sound_threat >= 1)  # enemies + sounds = more coming
        )

        # ENGAGE conditions (1v1 advantaged only)
        should_engage = (
            not should_flee and
            n_visible >= 1 and
            (
                (n_visible == 1 and hp_ratio > 0.50) or                # 1v1 and healthy
                (n_visible == 1 and enemy_mechs[0].get("hp", 1000) < hp * 0.7)  # enemy is weaker
            )
        )

        # ─── 7. Projectile Dodging ─────────────────────────────────
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0
        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            # Filter out projectiles moving away from us (our own shots / passed shots)
            if (pv[0] * (px - pp[0]) + pv[1] * (py - pp[1])) <= 0.0:
                continue
            pspeed = math.hypot(pv[0], pv[1], pv[2])
            if pspeed < 1.0:
                continue

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

        # ─── 8. Movement & Combat Execution ────────────────────────
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None
        in_direct_combat = False

        # === FLEE STATE ===
        if should_flee and enemy_mechs:
            self.flee_ticks += 1
            self.last_combat_tick = current_tick

            # Calculate centroid of all visible enemies to flee FROM
            cx = sum(e["pos"][0] for e in enemy_mechs) / len(enemy_mechs)
            cy = sum(e["pos"][1] for e in enemy_mechs) / len(enemy_mechs)
            flee_x, flee_y = norm2d(px - cx, py - cy)

            # Blend flee direction with zone pull (don't flee out of zone!)
            if outside_curr or not in_zone:
                move_x = to_zone_x * 0.85 + flee_x * 0.15
                move_y = to_zone_y * 0.85 + flee_y * 0.15
            else:
                # Mostly flee, but bias toward zone center enough to not leave
                zone_pull = 0.15 if dist_to_next < zr * 0.85 else 0.45
                move_x = flee_x * (1.0 - zone_pull) + to_zone_x * zone_pull
                move_y = flee_y * (1.0 - zone_pull) + to_zone_y * zone_pull

            # Still aim at nearest enemy (keep front armor facing them to avoid 1.5x rear crit)
            primary = enemy_mechs[0]
            tpos = primary["pos"]
            aim_yaw = math.atan2(tpos[1] - py, tpos[0] - px)
            aim_pitch = math.atan2(tpos[2] + 4.5 - (pz + 7.0), max(1.0, primary["_dist"]))

            # Only return fire if enemy is very close and we can't avoid them
            if n_very_close >= 1 and primary["_dist"] < 20.0:
                should_fire = True
                combat_target = primary
                in_direct_combat = True
                aim_yaw, aim_pitch = self._compute_lead_aim(tpos, primary.get("vel", [0,0,0]), pos)

        # === ENGAGE STATE (1v1 advantaged) ===
        elif should_engage and enemy_mechs:
            self.flee_ticks = 0
            self.last_combat_tick = current_tick
            in_direct_combat = True
            primary_enemy = enemy_mechs[0]
            combat_target = primary_enemy
            tpos = primary_enemy["pos"]
            tvel = primary_enemy.get("vel", [0, 0, 0])
            tdist = primary_enemy["_dist"]

            aim_yaw, aim_pitch = self._compute_lead_aim(tpos, tvel, pos)
            should_fire = True

            away_x, away_y = norm2d(px - tpos[0], py - tpos[1])
            tx, ty = rotate90(away_x, away_y, self.strafe_dir)

            if tdist < 18.0:
                # Too close: backpedal while strafing
                move_x = away_x * 0.70 + tx * 0.20 + to_zone_x * 0.10
                move_y = away_y * 0.70 + ty * 0.20 + to_zone_y * 0.10
            elif tdist <= 35.0:
                # Optimal kiting range: circle-strafe
                move_x = tx * 0.65 + to_zone_x * 0.20 + away_x * 0.15
                move_y = ty * 0.65 + to_zone_y * 0.20 + away_y * 0.15
            else:
                # Long range: orbit at distance, don't rush in
                move_x = tx * 0.55 + to_zone_x * 0.30 + away_x * 0.15
                move_y = ty * 0.55 + to_zone_y * 0.30 + away_y * 0.15

        # === SURVIVE STATE (default) ===
        else:
            self.flee_ticks = 0

            # Supply drop evaluation (only go if no nearby threats)
            best_drop = None
            if not threat_sounds:
                for d in drops:
                    ddist = dist2d(d["pos"], pos)
                    lands_in = d.get("lands_in", 999.0)
                    holder = d.get("holder")
                    if lands_in <= 10.0 and (holder is None or holder == me.get("id")):
                        if ddist < 60.0:
                            best_drop = d
                            break

            # Weapon scavenging (only when safe)
            target_weapon_pickup = None
            if not has_secondary and all_pickups and not enemy_mechs and not threat_sounds:
                pref = {"laser": 4, "lightning": 3, "mortar": 1}
                good_weapons = [p for p in all_pickups
                                if pref.get(p.get("kind", ""), 0) > 1
                                and dist2d(p["pos"], pos) < 70.0]
                if good_weapons:
                    target_weapon_pickup = max(good_weapons,
                                                key=lambda p: pref.get(p.get("kind", ""), 0))

            # Loose cube collection (only very close ones)
            near_cube = None
            if cubes_on_ground and not enemy_mechs and not threat_sounds:
                close_cubes = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 15.0]
                if close_cubes:
                    near_cube = min(close_cubes, key=lambda c: dist2d(c["pos"], pos))

            # Safe NPC farming (early game only, no nearby threats)
            safe_to_farm = (not sounds or all(s.get("dist") == "far" for s in sounds))

            if best_drop:
                dp = best_drop["pos"]
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

            elif npcs and match_time < 60.0 and cubes < 3 and safe_to_farm and npcs[0]["_dist"] < 25.0:
                npc = npcs[0]
                combat_target = npc
                aim_yaw, aim_pitch = self._compute_lead_aim(npc["pos"], npc.get("vel", [0,0,0]), pos)
                should_fire = True
                nx, ny = norm2d(px - npc["pos"][0], py - npc["pos"][1])
                ntx, nty = rotate90(nx, ny, self.strafe_dir)
                if npc["_dist"] < 15.0:
                    move_x, move_y = nx * 0.6 + ntx * 0.4, ny * 0.6 + nty * 0.4
                else:
                    move_x, move_y = -nx * 0.5 + ntx * 0.5, -ny * 0.5 + nty * 0.5

            elif crates and cubes < 2 and match_time < 45.0 and safe_to_farm:
                close_crates = [c for c in crates if dist2d(c["pos"], pos) < 20.0]
                if close_crates:
                    c = close_crates[0]
                    cp = c["pos"]
                    aim_yaw = math.atan2(cp[1] - py, cp[0] - px)
                    aim_pitch = math.atan2(cp[2] + 2.0 - (pz + 7.0), max(1.0, dist2d(cp, pos)))
                    should_fire = True
                    move_x, move_y = norm2d(cp[0] - px, cp[1] - py)
                else:
                    # Fall through to edge orbit
                    move_x, move_y = self._edge_orbit(px, py, zc, zr, to_zone_x, to_zone_y, dist_to_next)
                    move_x, move_y = self._sound_avoidance(move_x, move_y, to_zone_x, to_zone_y, sounds)
            else:
                # === EDGE ORBIT: stay at 85-95% zone radius ===
                move_x, move_y = self._edge_orbit(px, py, zc, zr, to_zone_x, to_zone_y, dist_to_next)

                # === SOUND AVOIDANCE: move AWAY from gunfire ===
                move_x, move_y = self._sound_avoidance(move_x, move_y, to_zone_x, to_zone_y, sounds)

        # ─── 9. Integrate Dodge Vectors ────────────────────────────
        if dodge_urgency > 0.05:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        # ─── 10. Zone Edge Escape Override ─────────────────────────
        if outside_curr:
            move_x = to_zone_x * 0.90 + move_x * 0.10
            move_y = to_zone_y * 0.90 + move_y * 0.10

        # ─── 11. Anti-Stuck ────────────────────────────────────────
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if moved < 0.14:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        self.last_pos = list(pos)

        if self.stuck_ticks > 1:
            move_x, move_y = rotate90(move_x, move_y, self.strafe_dir)
            if self.stuck_ticks > 5:
                self.strafe_dir *= -1
                move_x, move_y = -move_x, -move_y

        # Normalize movement
        move_x, move_y = norm2d(move_x, move_y)

        # Jump: ONLY when stuck (no periodic jumping — saves energy, less predictable)
        should_jump = (self.stuck_ticks > 1) and grounded

        # ─── 12. Boost Allocation ──────────────────────────────────
        should_boost = False
        if not in_zone or outside_curr:
            should_boost = True
        elif should_flee and energy > 15.0:
            # Always boost when fleeing
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 20.0:
            should_boost = True
        elif in_direct_combat and combat_target and combat_target.get("_dist", 50.0) < 14.0 and energy > 25.0:
            should_boost = True
        elif energy > 85.0 and dist_to_next > (zr * 0.80):
            should_boost = True

        # ─── 13. Weapons Firing ────────────────────────────────────
        fire_slot0 = False
        fire_slot1 = False

        if should_fire:
            w0 = weapons[0] if len(weapons) > 0 else None
            if w0 is not None:
                ammo = w0.get("ammo", 30)
                cd = w0.get("cooldown", 0.0)
                reloading = w0.get("reload", 0.0)
                if cd <= 0.0 and ammo > 0 and reloading <= 0.0:
                    fire_slot0 = True
                elif ammo <= 0 and reloading <= 0.0:
                    action["reload"] = True

            w1 = weapons[1] if len(weapons) > 1 else None
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                cd1 = w1.get("cooldown", 0.0)
                tdist = combat_target.get("_dist", 50.0) if combat_target else 50.0

                if w1_kind == "laser":
                    fire_slot1 = True
                elif w1_kind == "lightning" and tdist < 45.0:
                    fire_slot1 = (cd1 <= 0.0)
                elif w1_kind == "mortar" and 25.0 < tdist < 85.0:
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

    # ─── Helper: Edge Orbit ────────────────────────────────────────
    def _edge_orbit(self, px, py, zc, zr, to_zone_x, to_zone_y, dist_to_next):
        """Orbit at 85-95% zone radius — stay near edge, avoid center."""
        if dist_to_next < (zr * 0.70):
            # Too close to center: push outward aggressively
            out_x, out_y = norm2d(px - zc[0], py - zc[1])
            if out_x == 0.0 and out_y == 0.0:
                out_x, out_y = 1.0, 0.0  # arbitrary direction if exactly at center
            tx, ty = rotate90(out_x, out_y, self.strafe_dir)
            return out_x * 0.65 + tx * 0.35, out_y * 0.65 + ty * 0.35
        elif dist_to_next > (zr * 0.93):
            # Nearing zone boundary: steer inward
            return to_zone_x * 0.7 + rotate90(to_zone_x, to_zone_y, self.strafe_dir)[0] * 0.3, \
                   to_zone_y * 0.7 + rotate90(to_zone_x, to_zone_y, self.strafe_dir)[1] * 0.3
        else:
            # In sweet spot (70-93% radius): orbit around perimeter
            tx, ty = rotate90(to_zone_x, to_zone_y, self.strafe_dir)
            return tx * 0.80 + to_zone_x * 0.20, ty * 0.80 + to_zone_y * 0.20

    # ─── Helper: Sound Avoidance ───────────────────────────────────
    def _sound_avoidance(self, move_x, move_y, to_zone_x, to_zone_y, sounds):
        """Steer AWAY from nearby gunfire sounds instead of toward them."""
        for s in sounds:
            if s.get("dist") in ("near", "mid") and s.get("kind") in ("gun", "mortar", "laser", "lightning"):
                bearing = s.get("bearing")
                if bearing is not None:
                    # Run opposite to the sound source
                    away_yaw = bearing + math.pi
                    avoid_x = math.cos(away_yaw)
                    avoid_y = math.sin(away_yaw)

                    # Strength based on distance: near = strong avoidance, mid = moderate
                    strength = 0.55 if s["dist"] == "near" else 0.30
                    move_x = move_x * (1.0 - strength) + avoid_x * strength
                    move_y = move_y * (1.0 - strength) + avoid_y * strength
                    break  # Only react to the most immediate threat sound
        return move_x, move_y

    # ─── Lead Aim Prediction ───────────────────────────────────────
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
        final_z = tpz + tvz * lead_t + 4.5

        yaw = math.atan2(final_y - mpy, final_x - mpx)
        hdist = max(1.0, dist2d((final_x, final_y), (mpx, mpy)))
        pitch = math.atan2(final_z - (mpz + 7.0), hdist)
        return yaw, pitch


if __name__ == "__main__":
    mf.run(ApexHunter)
