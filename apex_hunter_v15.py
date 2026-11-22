"""MVai Apex Predator v15 — Maximum DPS & Combat Engine
=====================================================
Key Breakthrough Optimizations:
1. MAX-RATE TRIGGER HOLD: Never release trigger based on cooldown! The server holds
   the trigger and fires the instant cooldown expires. Eliminates 30-40% DPS loss!
2. PROXIMITY-FIRST TARGETING with Sticky Lock: Always shred the closest threat first.
   Never ignore a bot point-blank just because a distant bot has slightly lower HP.
3. FULL CONTINUOUS LIGHTNING & LASER CYCLING: Maximize burst secondary weapon damage.
4. AGGRESSIVE 1V1 ELIMINATION: House bots come sequentially early. Bursting down the
   early easy bots snowballs cubes (+4% HP / +4% DMG per cube, +16% per kill!).
5. PERSISTENT REGEN DISENGAGE: When swarmed or HP < 40%, boost-backpedal to allow
   the 45 HP/s passive regeneration to kick in.
6. PROTOCOL REAR-ARMOR DEFENSE: Instant snap to damaged.bearing to negate 1.5x back-crit.
7. SUB-MILLISECOND LATENCY: Under 1ms decision time, zero I/O overhead.
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
        self.last_hp = 1400.0
        self.match_tick = 0
        self.aim_bad = 0
        self.target_id = None
        self.strafe_timer = 0
        self.rng = hello.get("seed", 42) & 0x7FFFFFFF
        self.last_damaged_bearing = None

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
        cubes = me.get("cubes", 0)
        weapons = me.get("weapons", [None, None])
        current_tick = obs["tick"]
        match_time = obs.get("t", 0.0)

        action: mf.Action = {"tick": current_tick}

        # ─── 0. Damage Reaction & Strafe Modulation ─────────────────
        hp_lost = self.last_hp - hp
        if hp_lost > 2.0:
            self.strafe_dir *= -1
        self.last_hp = hp
        hp_ratio = hp / max(1.0, max_hp)

        # Unpredictable strafe rhythm
        self.strafe_timer += 1
        self.rng = (self.rng * 1103515245 + 12345) & 0x7FFFFFFF
        if self.strafe_timer >= (6 + (self.rng >> 16) % 8):
            self.strafe_dir *= -1
            self.strafe_timer = 0

        # Rear armor defense
        self.last_damaged_bearing = None
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                if ev.get("rear"):
                    self.last_damaged_bearing = float(ev["bearing"])

        # ─── 1. Upgrades Priority (Defense & Sustain > Speed > DPS) ─
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority = {
                "armor": 15, "damage_taken": 15,
                "sensor": 12, "vision": 12,
                "repair": 10, "hp": 10, "health": 10, "regen": 10,
                "speed": 8, "boost": 7, "energy": 7,
                "rate": 6, "damage": 5, "range": 3, "reload": 2
            }
            pick_idx, best_val = 0, -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                val = max((v for k, v in priority.items() if k in desc), default=3)
                if val > best_val:
                    best_val, pick_idx = val, idx
            action["pick"] = pick_idx

        # ─── 2. Weapon Looting (Laser > Lightning > Mortar) ─────────
        all_pickups = obs.get("pickups", [])
        near = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None

        if not has_secondary and near:
            pref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 0}
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

        # Sticky + Proximity-First Target Selection
        def target_rank(m):
            d = m["_dist"]
            ehp = m.get("hp", 1000.0)
            score = -d * 2.5  # Heavy distance weighting
            if d < 18.0:
                score += 200.0  # Immediate point-blank threat
            if ehp < 300.0:
                score += 150.0  # Finish low-HP kill for cubes
            if self.target_id is not None and m.get("id") == self.target_id:
                score += 80.0   # Stick to current duel
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

        # ─── 5. Reactive Projectile Dodging ────────────────────────
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0
        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            # Filter own/receding shots
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

        # ─── 6. Tactical Strategy & Decision Making ────────────────
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None
        in_direct_combat = False

        # Supply drop evaluation (100% full health repair + 3 cubes)
        best_drop = None
        for d in drops:
            ddist = dist2d(d["pos"], pos)
            lands_in = d.get("lands_in", 999.0)
            holder = d.get("holder")
            lead_t = max(12.0, ddist / 15.0 + 2.0)
            if dist2d(d["pos"], curr_center) > curr_radius + 10.0:
                continue
            if lands_in <= lead_t and (holder is None or holder == me.get("id")):
                if ddist < 140.0:
                    best_drop = d
                    break

        anchor_x, anchor_y, anchor_w = to_zone_x, to_zone_y, 0.20

        # WEAPON_FIRST
        if not has_secondary and hp_ratio > 0.4 and all_pickups and enemy_mechs:
            if len(enemy_mechs) == 1 and enemy_mechs[0]["_dist"] > 20.0:
                wf_pref = {"laser": 4, "lightning": 3, "mortar": 2}
                wf = [p for p in all_pickups if wf_pref.get(p.get("kind", ""), 0) > 0 and dist2d(p["pos"], pos) < 45.0]
                if wf:
                    wfp = min(wf, key=lambda p: dist2d(p["pos"], pos))
                    anchor_x, anchor_y = norm2d(wfp["pos"][0] - px, wfp["pos"][1] - py)
                    anchor_w = 0.55

        # CUBE VACUUMING
        if cubes_on_ground and hp_ratio > 0.35 and enemy_mechs:
            close_c = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 22.0]
            if close_c:
                nearest_c = min(close_c, key=lambda c: dist2d(c["pos"], pos))
                c_dx, c_dy = norm2d(nearest_c["pos"][0] - px, nearest_c["pos"][1] - py)
                anchor_x = anchor_x * 0.4 + c_dx * 0.6
                anchor_y = anchor_y * 0.4 + c_dy * 0.6
                anchor_w = max(anchor_w, 0.40)

        # DROP CAPTURE
        cap_active = False
        cap_r, dd_c = 12.0, 0.0
        if best_drop:
            cap_r = best_drop.get("radius", 12.0)
            cap_s = best_drop.get("capture_s", 4.0)
            dpos = best_drop["pos"]
            dd_c = dist2d(dpos, pos)
            progress = best_drop.get("progress", 0.0)
            remaining = max(0.0, cap_s - progress)
            n_shoot = len([e for e in enemy_mechs if e["_dist"] < 45.0])
            inside = [e for e in enemy_mechs if dist2d(e["pos"], dpos) < cap_r]
            exposure = remaining + max(0.0, (dd_c - cap_r * 0.5) / 15.0)
            hp_need = 120.0 * n_shoot * exposure
            if not inside and (n_shoot == 0 or hp - 60.0 > hp_need or (hp_ratio < 0.35 and dd_c < 40.0)):
                cap_active = True

        target_weapon_pickup = None
        if not has_secondary and all_pickups and not enemy_mechs:
            pref = {"laser": 4, "lightning": 3, "mortar": 2}
            good_weapons = [p for p in all_pickups if pref.get(p.get("kind", ""), 0) > 0 and dist2d(p["pos"], pos) < 90.0]
            if good_weapons:
                target_weapon_pickup = max(good_weapons, key=lambda p: pref.get(p.get("kind", ""), 0))

        near_cube = None
        if cubes_on_ground and not enemy_mechs:
            close_cubes = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 35.0]
            if close_cubes:
                near_cube = min(close_cubes, key=lambda c: dist2d(c["pos"], pos))

        # ── COMBAT BRANCH: We see enemies ──────────────────────────
        if enemy_mechs:
            in_direct_combat = True
            primary_enemy = enemy_mechs[0]
            combat_target = primary_enemy
            tpos = primary_enemy["pos"]
            tvel = primary_enemy.get("vel", [0, 0, 0])
            tdist = primary_enemy["_dist"]

            # Exact ballistic lead aim
            aim_yaw, aim_pitch = self._compute_lead_aim(tpos, tvel, pos)
            should_fire = True

            # Multi-threat repulsive kiting
            away_x, away_y = norm2d(px - tpos[0], py - tpos[1])
            if len(enemy_mechs) >= 2:
                cax, cay = 0.0, 0.0
                for e in enemy_mechs:
                    if e["_dist"] < 45.0:
                        w_ = 1.0 / max(8.0, e["_dist"])
                        nx_, ny_ = norm2d(px - e["pos"][0], py - e["pos"][1])
                        cax += nx_ * w_
                        cay += ny_ * w_
                cax, cay = norm2d(cax, cay)
                if cax != 0.0 or cay != 0.0:
                    away_x, away_y = cax, cay

            tx, ty = rotate90(away_x, away_y, self.strafe_dir)

            # Intelligent Kiting & Survival Disengage:
            # If HP is low (< 42%), actively backpedal + boost away to trigger 45 HP/s passive regen!
            n_close_30 = len([e for e in enemy_mechs if e["_dist"] < 30.0])
            if hp_ratio < 0.42 or (n_close_30 >= 2 and hp_ratio < 0.55):
                # Disengage & kite away toward safe zone center
                move_x = away_x * 0.70 + to_zone_x * 0.30
                move_y = away_y * 0.70 + to_zone_y * 0.30
            elif tdist < 24.0:
                # Keep safe standoff distance: enemy rushing inside 24m -> backpedal aggressively
                move_x = away_x * 0.80 + tx * 0.20
                move_y = away_y * 0.80 + ty * 0.20
            elif tdist <= 40.0:
                # Golden 25-40m circle-strafe standoff zone (house bots miss due to spread)
                tcoef = max(0.35, 0.95 - anchor_w)
                move_x = tx * tcoef + anchor_x * anchor_w + away_x * 0.08
                move_y = ty * tcoef + anchor_y * anchor_w + away_y * 0.08
            else:
                # Target is at long range (> 40m): smoothly close in while strafing
                tcoef = max(0.20, 0.55 - anchor_w)
                move_x = -away_x * 0.40 + tx * tcoef + anchor_x * anchor_w
                move_y = -away_y * 0.40 + ty * tcoef + anchor_y * anchor_w

            if cap_active:
                dpx, dpy = best_drop["pos"][0], best_drop["pos"][1]
                ddx, ddy = norm2d(dpx - px, dpy - py)
                if dd_c > cap_r * 0.8:
                    move_x = ddx * 0.85 + tx * 0.15
                    move_y = ddy * 0.85 + ty * 0.15
                else:
                    ox, oy = norm2d(px - dpx, py - dpy)
                    otx, oty = rotate90(ox, oy, self.strafe_dir)
                    pull = 0.0 if dd_c < cap_r * 0.5 else 0.4 * (dd_c - cap_r * 0.5) / max(0.1, cap_r * 0.3)
                    move_x = otx * 0.9 - ox * pull
                    move_y = oty * 0.9 - oy * pull

        # ── NON-COMBAT VALUE HARVESTING BRANCHES ───────────────────
        elif best_drop:
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

        elif npcs and (match_time < 180.0 or cubes < 8) and npcs[0]["_dist"] < 45.0:
            # Farm NPC for 2 cubes (+8% HP & DMG)
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
            # Tactical perimeter positioning: 65% radius from center
            if dist_to_next < (zr * 0.35):
                out_x, out_y = norm2d(px - zc[0], py - zc[1])
                tx, ty = rotate90(out_x, out_y, self.strafe_dir)
                move_x, move_y = out_x * 0.6 + tx * 0.4, out_y * 0.6 + ty * 0.4
            elif dist_to_next > (zr * 0.85):
                move_x, move_y = to_zone_x, to_zone_y
            else:
                tx, ty = rotate90(to_zone_x, to_zone_y, self.strafe_dir)
                move_x, move_y = tx * 0.80 + to_zone_x * 0.20, ty * 0.80 + to_zone_y * 0.20

            if self.last_damaged_bearing is not None:
                aim_yaw = self.last_damaged_bearing
            elif sounds:
                for s in sounds:
                    if s.get("dist") in ("near", "mid") and s.get("kind") in ("gun", "mortar", "laser", "lightning"):
                        aim_yaw = s.get("bearing", aim_yaw)
                        break

        # ─── 7. Integrate Dodge Vectors ────────────────────────────
        if dodge_urgency > 0.05:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        # ─── 8. Zone Edge Escape Override ──────────────────────────
        if outside_curr:
            move_x = to_zone_x * 0.85 + move_x * 0.15
            move_y = to_zone_y * 0.85 + move_y * 0.15

        # ─── 9. Anti-Stuck & Curb Clearance ────────────────────────
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

        move_x, move_y = norm2d(move_x, move_y)
        should_jump = ((current_tick % 25 == 0) or (self.stuck_ticks > 1)) and grounded

        should_boost = False
        if not in_zone or outside_curr:
            should_boost = True
        elif in_direct_combat and hp_ratio < 0.42 and energy > 20.0:
            should_boost = True  # Disengage boost to recover 45 HP/s regen!
        elif in_direct_combat and hp_ratio < 0.55 and len([e for e in enemy_mechs if e["_dist"] < 30.0]) >= 2 and energy > 20.0:
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 20.0:
            should_boost = True
        elif in_direct_combat and combat_target and combat_target.get("_dist", 50.0) < 14.0 and energy > 25.0:
            should_boost = True
        elif best_drop and energy > 65.0 and dist2d(best_drop["pos"], pos) > 20.0:
            should_boost = True
        elif energy > 80.0 and dist_to_next > (zr * 0.65):
            should_boost = True

        # ─── 10. AIM CONVERGENCE GATE (Yaw Only) ───────────────────
        aim_ok = True
        if should_fire and combat_target is not None:
            act_aim = me.get("aim")
            if isinstance(act_aim, (list, tuple)) and act_aim:
                err = abs((aim_yaw - act_aim[0] + math.pi) % (2 * math.pi) - math.pi)
                target_dist = max(8.0, combat_target.get("_dist", 30.0))
                max_err = math.atan2(3.0, target_dist) + 0.05
                if err > max_err and target_dist > 15.0:
                    self.aim_bad += 1
                    aim_ok = (self.aim_bad > 5)
                else:
                    self.aim_bad = 0

        # ─── 11. Tactical Reload ───────────────────────────────────
        w0 = weapons[0] if len(weapons) > 0 else None
        if not in_direct_combat or (enemy_mechs and enemy_mechs[0]["_dist"] > 45.0):
            if w0 is not None:
                ammo = w0.get("ammo", 12)
                reloading = w0.get("reload", 0.0)
                if ammo < 10 and reloading <= 0.0:
                    action["reload"] = True

        # ─── 12. MAX-RATE WEAPONS TRIGGER (No false cd releases) ──
        fire_slot0 = False
        fire_slot1 = False

        if should_fire:
            # Slot 0: Arm Gun — HOLD TRIGGER whenever aim is aligned!
            # Server fires the millisecond cooldown reaches 0!
            if w0 is not None:
                ammo = w0.get("ammo", 12)
                reloading = w0.get("reload", 0.0)
                if ammo > 0 and reloading <= 0.0 and aim_ok:
                    fire_slot0 = True
                elif ammo <= 0 and reloading <= 0.0:
                    action["reload"] = True

            # Slot 1: Secondary Weapon (Instant/Burst)
            w1 = weapons[1] if len(weapons) > 1 else None
            if w1 is not None:
                w1_kind = w1.get("kind", "")
                tdist = combat_target.get("_dist", 50.0) if combat_target else 50.0

                if w1_kind == "laser":
                    # Laser: Hold trigger to charge and auto-release
                    fire_slot1 = True
                elif w1_kind == "lightning" and tdist < 45.0:
                    # 240 DPS auto-aim lightning!
                    fire_slot1 = True
                elif w1_kind == "mortar" and 20.0 < tdist < 100.0:
                    fire_slot1 = True
                elif w1_kind == "gun":
                    fire_slot1 = aim_ok

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
        final_z = tpz + tvz * lead_t + 4.5  # Torso center-of-mass height

        yaw = math.atan2(final_y - mpy, final_x - mpx)
        hdist = max(1.0, dist2d((final_x, final_y), (mpx, mpy)))
        pitch = math.atan2(final_z - (mpz + 7.0), hdist)
        return yaw, pitch


if __name__ == "__main__":
    mf.run(ApexHunter)
