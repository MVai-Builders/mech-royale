"""MVai Apex Predator v7.3 = v7 (2.69) + telemetry + OPT-IN flags (5 new in v7.3).

With every F_* flag False this plays EXACTLY like v7 (telemetry only observes).
Turn flags on ONE AT A TIME and run >=2 evals (48 matches each) per flag.
"""
import json
import math
import os
import random
import sys
import mechfight as mf

# ---- feature flags: all OFF = v7 behavior ----
TELEMETRY = True        # observe only; never changes actions
F_DROP_BLEND = False    # combat stays first; drop becomes the kiting anchor (no stand-still, no early charge)
F_LASER_CYCLE = False   # DO NOT ENABLE: SDK says the laser auto-fires at full charge, so v7 holding is fine
F_SLOW_STRAFE = False   # extra strafe flip every 10-25 ticks (v7 flips only on damage)
F_UPGRADE_DMG = False   # damage/rate/reload above sensor in upgrade picks
F_SEARCH_TURN = False   # turn toward unseen attacker after damage with no enemy visible
F_FILTER_OWN = False    # ignore projectiles moving away from us in the dodge logic (own bullets / passed shots)
F_NO_MORTAR = False     # never pick up a mortar (flat-aimed mortar wastes the only secondary slot)
F_AWAY_CENTROID = False # with 2+ enemies visible, retreat/kite away from their weighted centroid, not just the primary
F_WEAPON_FIRST = False  # while no secondary: pull kiting toward a laser/lightning pickup within 45 m; scavenge out to 90 m
F_DROP_CAPTURE = False   # capture drops during fights when HP budget allows; orbit INSIDE the radius while firing
F_SWARM = False         # 2+ enemies within 30 m: retreat at HP<50% (v7: <25%) and boost away
INCOMING_DPS_PER_BOT = 120.0   # rough per-bot damage rate; replace with measured taken_amt from telemetry
DMG_BEARING_RELATIVE = False   # VERIFY in SDK: is damaged.bearing a world-frame yaw (False) or relative to torso (True)?
LASER_CHARGE_S = 1.2    # fallback if config has no laser charge_time. VERIFY.
TEL_PATH = "/tmp/mech_tel_%d.jsonl" % os.getpid()
TEL_MAX_BYTES = 4_000_000
# Compact summary to STDERR for when /tmp is unreachable (remote scoring). DEFAULT OFF.
# Risk: if the harness never drains stderr, a full pipe (~64 KB) could block the bot. Hard cap is 40 KB total.
# Turn on only for a dedicated submission, after confirming the portal shows bot stderr.
TEL_STDERR = True
TEL_STDERR_EVERY = 100
TEL_STDERR_MAX = 40_000

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

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1
        self.last_hp = 1000.0
        self.match_tick = 0

        self.rng = random.Random(11 + self.slot if isinstance(self.slot, int) else 11)
        self.laser_charge = cfg.get("weapons", {}).get("laser", {}).get("charge_time", LASER_CHARGE_S)
        self.flip_at = 0
        self.dmg_tick = -999
        self.last_enemy = None
        self.last_seen_tick = -999
        self.search_until = -1
        self.search_yaw = 0.0
        self.aim_prev = 0.0
        self.laser_held = 0
        self.tel_bytes = 0
        self.match_idx = 0
        self.last_tick_seen = -1
        self.schema = {"config": cfg, "map": hello.get("map")}
        self.cfg_dump = cfg
        self.dmg_bearing = None
        self.dmg_bearing_tick = -999
        self.cap_start = None
        self.cap_mode = False
        self.err_bytes = 0
        self.err_dead = False
        self.err_cfg_sent = False
        self.schema_sent = set()
        self._reset_tm()

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

        # 0. Damage reaction: flip strafe direction to break enemy predictive lead
        hp_lost = self.last_hp - hp
        if hp_lost > 2.0:
            self.strafe_dir *= -1
            if in_zone:
                self.dmg_tick = current_tick
        if F_SLOW_STRAFE and current_tick >= self.flip_at:
            self.strafe_dir *= -1
            self.flip_at = current_tick + self.rng.randint(10, 25)
        self.last_hp = hp
        hp_ratio = hp / max(1.0, max_hp)

        # 1. Upgrade selection: Armor > Sensor > HP/Repair > Speed > Damage
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority = {
                "armor": 12, "damage_taken": 12,
                "sensor": 10, "vision": 10,
                "repair": 9, "hp": 9, "health": 9, "regen": 9,
                "speed": 7, "boost": 6, "energy": 6,
                "rate": 5, "damage": 4, "range": 3, "reload": 2
            }
            if F_UPGRADE_DMG:
                priority.update({"damage": 11, "rate": 10, "reload": 8})
            pick_idx, best_val = 0, -1
            for idx, c in enumerate(choices):
                desc = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                val = max((v for k, v in priority.items() if k in desc), default=3)
                if val > best_val:
                    best_val, pick_idx = val, idx
            action["pick"] = pick_idx

        # 2. Looting secondary weapon if within reach (4m)
        all_pickups = obs.get("pickups", [])
        near = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.reach]
        has_secondary = len(weapons) > 1 and weapons[1] is not None
        if not has_secondary and near:
            pref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}
            if F_NO_MORTAR:
                pref["mortar"] = 0
            best_pickup = max(near, key=lambda p: pref.get(p.get("kind", ""), 0))
            if pref.get(best_pickup.get("kind", ""), 0) > 0:
                action["take"] = best_pickup["id"]

        # 3. Zone vectors
        zone = obs.get("zone", {})
        zc = zone.get("next_center") or zone.get("center", [0.0, 0.0])
        zr = zone.get("next_radius") or zone.get("radius", 300.0)
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)
        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)
        outside_curr = dist_to_curr > (curr_radius * 0.96)

        # 4. Entity perception & sorting
        all_mechs = obs.get("mechs", [])
        enemy_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        for e in enemy_mechs:
            e["_dist"] = dist2d(e["pos"], pos)
        # Prioritize lowest HP if close (<35m), else nearest
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
        self._tel_events(obs, me, all_mechs, enemy_mechs, current_tick)
        self._scan_events(obs, me, current_tick)

        # 5. Projectile dodging
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0
        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            if F_FILTER_OWN and (pv[0] * (px - pp[0]) + pv[1] * (py - pp[1])) <= 0.0:
                continue
            pspeed = math.hypot(pv[0], pv[1], pv[2])
            if pspeed < 1.0: continue
            dx, dy = px - pp[0], py - pp[1]
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

        # 6. Strategic State Selection
        move_x, move_y = to_zone_x, to_zone_y
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        should_fire = False
        combat_target = None
        in_direct_combat = False

        # Supply drop evaluation (12s anticipation)
        best_drop = None
        for d in drops:
            ddist = dist2d(d["pos"], pos)
            lands_in = d.get("lands_in", 999.0)
            holder = d.get("holder")
            lead_t = 12.0
            if F_DROP_CAPTURE:
                lead_t = max(12.0, ddist / 15.0 + 2.0)
                if dist2d(d["pos"], curr_center) > curr_radius + 10.0:
                    continue
            if lands_in <= lead_t and (holder is None or holder == me.get("id")):
                if ddist < (140.0 if (F_DROP_BLEND or F_DROP_CAPTURE) else 90.0):
                    best_drop = d
                    break

        # kiting anchor: zone by default (v7); drop when F_DROP_BLEND and one is eligible
        anchor_x, anchor_y, anchor_w = to_zone_x, to_zone_y, 0.20
        if F_DROP_BLEND and best_drop:
            adp = best_drop["pos"]
            anchor_x, anchor_y = norm2d(adp[0] - px, adp[1] - py)
            anchor_w = 0.50 if hp_ratio < 0.6 else 0.35

        # F_WEAPON_FIRST: kite toward a good secondary while we have none
        if F_WEAPON_FIRST and not has_secondary and hp_ratio > 0.4 and all_pickups and enemy_mechs:
            if len(enemy_mechs) == 1 and enemy_mechs[0]["_dist"] > 20.0:
                wf_pref = {"laser": 4, "lightning": 3}
                wf = [p for p in all_pickups if wf_pref.get(p.get("kind", ""), 0) > 0 and dist2d(p["pos"], pos) < 45.0]
                if wf:
                    wfp = min(wf, key=lambda p: dist2d(p["pos"], pos))
                    anchor_x, anchor_y = norm2d(wfp["pos"][0] - px, wfp["pos"][1] - py)
                    anchor_w = 0.55

        # F_DROP_CAPTURE: decide whether to go for the drop NOW (HP budget vs expected damage while capturing)
        cap_active = False
        cap_r, dd_c = 12.0, 0.0
        if F_DROP_CAPTURE and best_drop:
            cap_r = best_drop.get("radius", 12.0)
            cap_s = best_drop.get("capture_s", 4.0)
            dpos = best_drop["pos"]
            dd_c = dist2d(dpos, pos)
            if best_drop.get("holder") == me.get("id"):
                if self.cap_start is None:
                    self.cap_start = current_tick
            else:
                self.cap_start = None
            elapsed = (current_tick - self.cap_start) * 0.1 if self.cap_start is not None else 0.0
            remaining = max(0.0, cap_s - elapsed)
            n_shoot = len([e for e in enemy_mechs if e["_dist"] < 45.0])
            inside = [e for e in enemy_mechs if dist2d(e["pos"], dpos) < cap_r]
            exposure = remaining + max(0.0, (dd_c - cap_r * 0.5) / 15.0)
            hp_need = INCOMING_DPS_PER_BOT * n_shoot * exposure
            if not inside and (n_shoot == 0 or hp - 60.0 > hp_need or (hp_ratio < 0.35 and dd_c < 40.0)):
                cap_active = True
        else:
            self.cap_start = None
        self.cap_mode = cap_active

        # Weapon scavenging within 55m
        target_weapon_pickup = None
        if not has_secondary and all_pickups and not enemy_mechs:
            pref = {"laser": 4, "lightning": 3, "mortar": 2}
            if F_NO_MORTAR:
                pref["mortar"] = 0
            good_weapons = [p for p in all_pickups if pref.get(p.get("kind", ""), 0) > 0 and dist2d(p["pos"], pos) < (90.0 if F_WEAPON_FIRST else 55.0)]
            if good_weapons:
                target_weapon_pickup = max(good_weapons, key=lambda p: pref.get(p.get("kind", ""), 0))

        # Loose cube vacuuming
        near_cube = None
        if cubes_on_ground and not enemy_mechs:
            close_cubes = [c for c in cubes_on_ground if dist2d(c["pos"], pos) < 25.0]
            if close_cubes:
                near_cube = min(close_cubes, key=lambda c: dist2d(c["pos"], pos))

        # Combat execution
        if enemy_mechs:
            in_direct_combat = True
            primary_enemy = enemy_mechs[0]
            combat_target = primary_enemy
            tpos = primary_enemy["pos"]
            tvel = primary_enemy.get("vel", [0, 0, 0])
            tdist = primary_enemy["_dist"]

            # Always lock aim on threat (avoids 1.5x rear crit damage!)
            aim_yaw, aim_pitch = self._compute_lead_aim(tpos, tvel, pos)
            should_fire = True

            away_x, away_y = norm2d(px - tpos[0], py - tpos[1])
            if F_AWAY_CENTROID and len(enemy_mechs) >= 2:
                cax = cay = 0.0
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

            if len([e for e in enemy_mechs if e["_dist"] < 30.0]) >= 2 and hp_ratio < (0.50 if F_SWARM else 0.25):
                # Swarmed & low HP: backpedal towards zone
                move_x = away_x * 0.5 + to_zone_x * 0.5
                move_y = away_y * 0.5 + to_zone_y * 0.5
            elif tdist < 18.0:
                # Enemy rushing: backpedal while strafing
                move_x = away_x * 0.75 + tx * 0.25
                move_y = away_y * 0.75 + ty * 0.25
            elif tdist <= 35.0:
                # Optimal range: circle-strafe + zone drift
                tcoef = max(0.30, 0.95 - anchor_w)
                move_x = tx * tcoef + anchor_x * anchor_w + away_x * 0.05
                move_y = ty * tcoef + anchor_y * anchor_w + away_y * 0.05
            else:
                # Approach while strafing
                tcoef = max(0.15, 0.55 - anchor_w)
                move_x = -away_x * 0.45 + tx * tcoef + anchor_x * anchor_w
                move_y = -away_y * 0.45 + ty * tcoef + anchor_y * anchor_w

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
            # Orbit sweet-spot perimeter (65% zone radius)
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

        # 6b. unseen attacker: turn toward it (opt-in)
        if F_SEARCH_TURN and not enemy_mechs and not should_fire:
            if current_tick - self.dmg_tick <= 3 and current_tick > self.search_until:
                yaw = None
                if self.dmg_bearing is not None and current_tick - self.dmg_bearing_tick <= 3:
                    yaw = self.dmg_bearing
                for s in sounds:
                    if yaw is None and s.get("dist") in ("near", "mid") and "bearing" in s:
                        yaw = s["bearing"]
                        break
                if yaw is None and self.last_enemy is not None and current_tick - self.last_seen_tick < 40:
                    yaw = math.atan2(self.last_enemy[1] - py, self.last_enemy[0] - px)
                if yaw is None:
                    yaw = self.aim_prev + 2.4
                self.search_yaw, self.search_until = yaw, current_tick + 8
            if current_tick <= self.search_until:
                aim_yaw = self.search_yaw

        # 7. Apply dodge vector
        if dodge_urgency > 0.05:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            blend = min(0.85, dodge_urgency)
            move_x = move_x * (1.0 - blend) + dn_x * blend
            move_y = move_y * (1.0 - blend) + dn_y * blend

        # 8. Escape storm
        if outside_curr:
            move_x = to_zone_x * 0.85 + move_x * 0.15
            move_y = to_zone_y * 0.85 + move_y * 0.15

        # 9. Anti-stuck & curb jumping
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if moved < 0.14: self.stuck_ticks += 1
            else: self.stuck_ticks = 0
        self.last_pos = list(pos)

        if self.stuck_ticks > 1:
            move_x, move_y = rotate90(move_x, move_y, self.strafe_dir)
            if self.stuck_ticks > 5:
                self.strafe_dir *= -1
                move_x, move_y = -move_x, -move_y

        move_x, move_y = norm2d(move_x, move_y)
        should_jump = ((current_tick % 25 == 0) or (self.stuck_ticks > 1)) and grounded

        # Boost allocation
        should_boost = False
        if not in_zone or outside_curr:
            should_boost = True
        elif dodge_urgency > 0.45 and energy > 20.0:
            should_boost = True
        elif in_direct_combat and combat_target and combat_target.get("_dist", 50.0) < 14.0 and energy > 25.0:
            should_boost = True
        elif best_drop and energy > 65.0 and dist2d(best_drop["pos"], pos) > 20.0:
            should_boost = True
        elif F_SWARM and in_direct_combat and hp_ratio < 0.5 and energy > 20.0 \
                and len([e for e in enemy_mechs if e["_dist"] < 30.0]) >= 2:
            should_boost = True
        elif energy > 80.0 and dist_to_next > (zr * 0.65):
            should_boost = True

        # 10. Weapons firing
        fire_slot0 = False
        fire_slot1 = False
        laser_release = False
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
                    if F_LASER_CYCLE:
                        fire_slot1, laser_release = self._laser(tdist, cd1)
                    else:
                        fire_slot1 = True
                elif w1_kind == "lightning" and tdist < 45.0:
                    fire_slot1 = (cd1 <= 0.0)
                elif w1_kind == "mortar" and 25.0 < tdist < 85.0:
                    fire_slot1 = (cd1 <= 0.0)
                elif w1_kind == "gun":
                    fire_slot1 = (cd1 <= 0.0)

        if not should_fire:
            self.laser_held = 0
        if laser_release and combat_target is not None:
            aim_yaw, aim_pitch = self._compute_lead_aim(combat_target["pos"], [0.0, 0.0, 0.0], pos)

        self._tel_tick(obs, me, enemy_mechs, best_drop, target_weapon_pickup, near_cube,
                       should_fire, combat_target, fire_slot0, weapons, current_tick, aim_yaw)
        if enemy_mechs:
            self.last_enemy = list(enemy_mechs[0]["pos"])
            self.last_seen_tick = current_tick
        self.aim_prev = aim_yaw

        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1]
        )
        return action

    # ---------------- opt-in helpers ----------------
    def _laser(self, tdist, cd1):
        """Hold to charge, release one tick to fire. Returns (fire, released)."""
        if cd1 > 0.0 or tdist > 90.0:
            self.laser_held = 0
            return False, False
        if self.laser_held * 0.1 >= self.laser_charge + 0.05:
            self.laser_held = 0
            return False, True
        self.laser_held += 1
        return True, False

    def _scan_events(self, obs, me, tick):
        for ev in obs.get("events") or []:
            if isinstance(ev, dict) and ev.get("type") == "damaged" and isinstance(ev.get("bearing"), (int, float)):
                b = float(ev["bearing"])
                if DMG_BEARING_RELATIVE:
                    a = me.get("aim")
                    if isinstance(a, (list, tuple)) and a:
                        b += a[0]
                self.dmg_bearing = b
                self.dmg_bearing_tick = tick

    # ---------------- telemetry (observe only) ----------------
    def _reset_tm(self):
        self.tm = {
            "ev_count": {}, "ev_schema": {},
            "dealt_n": 0, "dealt_amt": 0.0, "dealt_rear_n": 0, "dealt_by_weapon": {},
            "taken_n": 0, "taken_amt": 0.0, "taken_rear_amt": 0.0, "taken_blind_amt": 0.0, "taken_by_weapon": {},
            "taken_by_attacker": {}, "kills": 0, "kill_ticks": [], "ev_samples": {},
            "aim_err_n": 0, "aim_err_sum": 0.0, "aim_err_big": 0, "w1_ticks": 0, "w1_kind": None, "drop_vis_ticks": 0, "cap_ticks": 0,
            "shots": [0, 0, 0, 0], "hits_b": [0, 0, 0, 0],
            "branch": {}, "stuck_ticks": 0, "max_vis": 0,
            "first_enemy_tick": None, "ticks": 0,
        }

    def _err(self, text):
        line = text + "\n"
        self.err_bytes += len(line)
        sys.stderr.write(line)
        sys.stderr.flush()

    @staticmethod
    def _bucket(d):
        return 0 if d < 20 else 1 if d < 40 else 2 if d < 60 else 3

    def _tel_events(self, obs, me, all_mechs, enemy_mechs, tick):
        if not TELEMETRY:
            return
        try:
            if tick < self.last_tick_seen:      # new match in same process
                self.err_dead = False
                self.match_idx += 1
                self._reset_tm()
            self.last_tick_seen = tick
            tm = self.tm
            tm["ticks"] += 1
            if enemy_mechs and tm["first_enemy_tick"] is None:
                tm["first_enemy_tick"] = tick
            tm["max_vis"] = max(tm["max_vis"], len(enemy_mechs))
            if "self" not in self.schema:
                self.schema["self"] = sorted(me.keys())
                if enemy_mechs: self.schema["enemy"] = sorted(enemy_mechs[0].keys())
                if obs.get("drops"): self.schema["drop"] = sorted(obs["drops"][0].keys())
                if obs.get("projectiles"): self.schema["proj"] = sorted(obs["projectiles"][0].keys())
                w = me.get("weapons") or []
                self.schema["weapons"] = [sorted(x.keys()) if isinstance(x, dict) else None for x in w]
                self.schema["obs_keys"] = sorted(obs.keys())
            elif "drop" not in self.schema and obs.get("drops"):
                self.schema["drop"] = sorted(obs["drops"][0].keys())
            elif "enemy" not in self.schema and enemy_mechs:
                self.schema["enemy"] = sorted(enemy_mechs[0].keys())
            dist_by_id = {m.get("id"): m.get("_dist") for m in all_mechs if m.get("id") is not None}
            for ev in obs.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                et = str(ev.get("type"))
                tm["ev_count"][et] = tm["ev_count"].get(et, 0) + 1
                if et not in tm["ev_schema"]:
                    tm["ev_schema"][et] = ev
                if et in ("drop", "upgrade", "pickup", "took", "take_failed", "dropped", "effect") \
                        and len(tm["ev_samples"].setdefault(et, [])) < 6:
                    tm["ev_samples"][et].append({"tick": tick, **ev})
                amt = ev.get("amount", 0.0)
                amt = float(amt) if isinstance(amt, (int, float)) else 0.0
                wpn = str(ev.get("weapon"))
                if et == "hit":                      # damage WE dealt (target is never us)
                    tm["dealt_n"] += 1
                    tm["dealt_amt"] += amt
                    tm["dealt_by_weapon"][wpn] = tm["dealt_by_weapon"].get(wpn, 0.0) + amt
                    if ev.get("rear"):
                        tm["dealt_rear_n"] += 1
                    d = dist_by_id.get(ev.get("target"))
                    if d is not None:
                        tm["hits_b"][self._bucket(d)] += 1
                elif et == "damaged":                # damage WE took
                    tm["taken_n"] += 1
                    tm["taken_amt"] += amt
                    tm["taken_by_weapon"][wpn] = tm["taken_by_weapon"].get(wpn, 0.0) + amt
                    if ev.get("rear"):
                        tm["taken_rear_amt"] += amt
                    if not enemy_mechs:
                        tm["taken_blind_amt"] += amt
                    by = str(ev.get("by"))
                    tm["taken_by_attacker"][by] = tm["taken_by_attacker"].get(by, 0.0) + amt
                elif et == "kill":
                    tm["kills"] += 1
                    if len(tm["kill_ticks"]) < 30:
                        tm["kill_ticks"].append(tick)
        except Exception:
            pass

    def _tel_tick(self, obs, me, enemy_mechs, best_drop, wpick, ncube, should_fire, ctarget, fire0, weapons, tick, aim_cmd):
        if not TELEMETRY:
            return
        try:
            tm = self.tm
            if enemy_mechs: br = "combat"
            elif best_drop: br = "drop"
            elif wpick: br = "weapon"
            elif ncube: br = "cube"
            elif should_fire and ctarget is not None and ctarget.get("kind") == "npc": br = "npc"
            elif should_fire: br = "crate"
            else: br = "orbit"
            tm["branch"][br] = tm["branch"].get(br, 0) + 1
            w1x = weapons[1] if len(weapons) > 1 else None
            if isinstance(w1x, dict):
                tm["w1_ticks"] += 1
                tm["w1_kind"] = w1x.get("kind")
            if obs.get("drops"):
                tm["drop_vis_ticks"] += 1
            if self.cap_mode:
                tm["cap_ticks"] += 1
            if self.stuck_ticks > 1:
                tm["stuck_ticks"] += 1
            if fire0 and enemy_mechs:
                tm["shots"][self._bucket(enemy_mechs[0]["_dist"])] += 1
                act_aim = me.get("aim")
                if isinstance(act_aim, (list, tuple)) and act_aim:
                    err = abs((aim_cmd - act_aim[0] + math.pi) % (2 * math.pi) - math.pi)
                    tm["aim_err_n"] += 1
                    tm["aim_err_sum"] += err
                    if err > 0.15:
                        tm["aim_err_big"] += 1
            if tick % 50 == 0 and self.tel_bytes < TEL_MAX_BYTES:
                w1 = weapons[1] if len(weapons) > 1 else None
                row = {
                    "m": self.match_idx, "tick": tick, "t": obs.get("t"), "hp": me.get("hp"), "cubes": me.get("cubes"),
                    "w1": (w1 or {}).get("kind") if isinstance(w1, dict) else None,
                    "vis": len(enemy_mechs), "near": round(min([e["_dist"] for e in enemy_mechs]), 1) if enemy_mechs else None,
                    "tm": tm,
                }
                new_schema = {k: v for k, v in self.schema.items() if k not in self.schema_sent}
                if new_schema:
                    row["schema"] = new_schema
                    self.schema_sent.update(new_schema.keys())
                line = json.dumps(row, default=str) + "\n"
                self.tel_bytes += len(line)
                with open(TEL_PATH, "a") as f:
                    f.write(line)
            if TEL_STDERR and self.err_bytes < TEL_STDERR_MAX:
                dead = obs.get("alive") is False
                if tick % TEL_STDERR_EVERY == 0 or (dead and not self.err_dead):
                    if dead:
                        self.err_dead = True
                    if not self.err_cfg_sent:
                        self.err_cfg_sent = True
                        self._err("CFG " + json.dumps(self.cfg_dump, default=str)[:10000])
                    keys = ("kills", "dealt_n", "dealt_amt", "dealt_by_weapon", "taken_n", "taken_amt",
                            "taken_rear_amt", "taken_blind_amt", "taken_by_weapon", "shots", "hits_b",
                            "branch", "stuck_ticks", "aim_err_n", "aim_err_sum", "aim_err_big",
                            "first_enemy_tick", "max_vis", "ev_count", "w1_ticks", "w1_kind", "drop_vis_ticks", "cap_ticks")
                    brief = {k: tm[k] for k in keys}
                    brief.update({"m": self.match_idx, "tick": tick, "hp": me.get("hp"),
                                  "cubes": me.get("cubes"), "alive": obs.get("alive")})
                    self._err("TEL " + json.dumps(brief, default=str, separators=(",", ":")))
        except Exception:
            pass

    def _compute_lead_aim(self, target_pos, target_vel, my_pos):
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
        final_z = tpz + tvz * lead_t + 4.5  # Target torso center height

        yaw = math.atan2(final_y - mpy, final_x - mpx)
        hdist = max(1.0, dist2d((final_x, final_y), (mpx, mpy)))
        pitch = math.atan2(final_z - (mpz + 7.0), hdist)
        return yaw, pitch

if __name__ == "__main__":
    mf.run(ApexHunter)
