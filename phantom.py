"""MVai Phantom v1 — Elite Survival Bot for Mech Royale
=======================================================
Design philosophy: SURVIVE, don't dominate.
  - Score = mean(16 - placement) over 48 matches.  Placement is ALL that matters.
  - 15 house bots share vision and cooperatively hunt weakest-first.
  - Fighting draws attention → more bots pile on → death.
  - Goal: be invisible, dodge everything, let others die, fight only when forced.

Key techniques:
  1. Projectile dodging — perpendicular burst when bullets approach
  2. Sound-based threat detection — face threats before they ambush
  3. Edge-of-zone positioning — stay safe but away from center cluster
  4. Circle-strafing in combat — never stand still
  5. Predictive aim with iterative lead — accurate even at range
  6. Smart engagement rules — flee when outnumbered or low HP
  7. NPC farming — cubes = stat advantage
  8. Supply drop awareness — massive power spikes
  9. Upgrade priority — armor > sensor > HP regen > everything else
  10. Anti-stuck — rhythmic jumping + heading rotation
"""

import math
import mechfight as mf


# ─── Utility functions ───────────────────────────────────────────────

def dist2d(a, b):
    """Horizontal distance between two positions."""
    return math.hypot(a[0] - b[0], a[1] - b[1])

def dist3d(a, b):
    """3D distance between two positions."""
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)

def norm2d(dx, dy):
    """Normalize a 2D vector, returns (0,0) if near-zero."""
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0, 0.0
    return dx / d, dy / d

def angle_diff(a, b):
    """Signed angular difference, normalized to [-pi, pi]."""
    d = a - b
    while d > math.pi: d -= 2 * math.pi
    while d < -math.pi: d += 2 * math.pi
    return d

def lerp(a, b, t):
    """Linear interpolation."""
    return a + (b - a) * t

def rotate90(dx, dy, direction=1):
    """Rotate a 2D vector 90 degrees. direction=1 for CCW, -1 for CW."""
    return -dy * direction, dx * direction


# ─── Main Bot Class ──────────────────────────────────────────────────

class PhantomBot:
    name = "MVai-Phantom"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.pickup_radius = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.slot = hello.get("slot", 0)
        self.seed = hello.get("seed", 42)

        # Weapon config from hello — projectile speeds
        weap_cfg = cfg.get("weapons", {})
        self.gun_speed = weap_cfg.get("gun", {}).get("speed", 252.0)
        self.mortar_speed = weap_cfg.get("mortar", {}).get("speed", 60.0)

        # Mech config
        mech_cfg = cfg.get("mech", {})
        self.walk_speed = mech_cfg.get("walk_speed", 11.7)
        self.boost_speed = mech_cfg.get("boost_speed", 19.5)
        self.rear_mult = mech_cfg.get("rear_mult", 1.5)

        # Vision config
        vis_cfg = cfg.get("vision", {})
        self.vision_range = vis_cfg.get("range", 160.0)
        self.vision_fov = vis_cfg.get("fov_deg", 120.0)

        # Zone config
        zone_cfg = cfg.get("zone", {})
        self.zone_phases = zone_cfg.get("phases", [])

        # Drop config
        drop_cfg = cfg.get("drops", {})
        self.drop_capture_s = drop_cfg.get("capture_s", 4.0)
        self.drop_cubes = drop_cfg.get("cubes", 3)

        # Cube upgrade thresholds
        cube_cfg = cfg.get("cubes", {})
        self.upgrade_thresholds = cube_cfg.get("upgrade_thresholds", [3, 7, 12, 18, 25])

        # State tracking
        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1   # +1 or -1
        self.last_hp = 1000.0
        self.last_max_hp = 1000.0
        self.tick_count = 0
        self.rng_state = self.seed & 0xFFFFFFFF

        # Threat tracking — remember damage sources
        self.damage_bearing = None     # yaw toward last attacker
        self.damage_tick = -100        # tick of last damage
        self.kill_count = 0

        # Phase tracking
        self.last_alive = 16

        # Enemy memory — track last known positions of enemies heard but not seen
        self.sound_threats = []  # [(bearing, dist_class, age)]

    def _rng(self):
        """Simple xorshift32 RNG seeded from hello.seed for deterministic behavior."""
        self.rng_state ^= (self.rng_state << 13) & 0xFFFFFFFF
        self.rng_state ^= (self.rng_state >> 17)
        self.rng_state ^= (self.rng_state << 5) & 0xFFFFFFFF
        return (self.rng_state & 0xFFFFFFFF) / 0xFFFFFFFF

    def act(self, obs: mf.Observation) -> mf.Action:
        self.tick_count += 1
        me = obs["self"]
        pos = me["pos"]
        px, py, pz = pos
        vel = me.get("vel", [0, 0, 0])
        hp = me["hp"]
        max_hp = me["max_hp"]
        energy = me["energy"]
        max_energy = me["max_energy"]
        in_zone = me["in_zone"]
        grounded = me["grounded"]
        cubes = me.get("cubes", 0)
        my_aim = me.get("aim", [0, 0])
        my_stats = me.get("stats", {})
        weapons = me["weapons"]
        current_tick = obs["tick"]
        match_time = obs.get("t", 0.0)
        alive_count = obs.get("alive", 16)

        action: mf.Action = {"tick": current_tick}

        # ─── Track events ─────────────────────────────────────────
        events = obs.get("events", [])
        took_damage = False
        damage_amount = 0.0
        for ev in events:
            etype = ev.get("type")
            if etype == "damaged":
                took_damage = True
                damage_amount += ev.get("amount", 0)
                b = ev.get("bearing")
                if b is not None:
                    self.damage_bearing = b
                    self.damage_tick = current_tick
            elif etype == "kill":
                self.kill_count += 1

        # Flip strafe direction when taking significant damage
        if took_damage and damage_amount > 15:
            self.strafe_dir *= -1

        hp_ratio = hp / max(1, max_hp)
        self.last_hp = hp
        self.last_max_hp = max_hp
        self.last_alive = alive_count

        # ─── Game phase ───────────────────────────────────────────
        # early = first 90s, mid = 90-240s, late = 240s+
        if match_time < 90:
            phase = "early"
        elif match_time < 240:
            phase = "mid"
        else:
            phase = "late"

        # ─── 1. Upgrade selection ─────────────────────────────────
        if obs.get("offer"):
            choices = obs["offer"].get("choices", [])
            priority_map = {
                "armor": 15, "damage_taken": 15,
                "sensor": 13, "vision": 13, "range": 12,
                "repair": 11, "regen": 11,
                "hp": 10, "health": 10, "max_hp": 10,
                "speed": 8, "walk": 8, "boost": 7,
                "rate": 6, "fire_rate": 6, "cooldown": 6,
                "damage": 5, "dmg": 5,
                "reload": 4, "ammo": 3,
                "energy": 9, "max_energy": 9,
            }
            best_idx, best_score = 0, -1
            for idx, c in enumerate(choices):
                text = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("desc", "")).lower()
                score = 0
                for key, val in priority_map.items():
                    if key in text:
                        score = max(score, val)
                if score > best_score:
                    best_score = score
                    best_idx = idx
            action["pick"] = best_idx

        # ─── 2. Loot management ──────────────────────────────────
        all_pickups = obs.get("pickups", [])
        near_pickups = [p for p in all_pickups if dist2d(p["pos"], pos) <= self.pickup_radius]

        if near_pickups:
            has_secondary = weapons[1] is not None
            if not has_secondary:
                # Prefer laser > lightning > mortar > gun
                wpref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 0}
                best = max(near_pickups, key=lambda p: wpref.get(p.get("kind", ""), 0))
                if wpref.get(best.get("kind", ""), 0) > 0:
                    action["take"] = best["id"]
            # Also pick up cubes (they appear as pickups with kind="cube"... actually cubes are in obs["cubes"])

        # Pick up cubes that are right next to us
        nearby_cubes = [c for c in obs.get("cubes", []) if dist2d(c["pos"], pos) <= self.pickup_radius + 1]
        # Cubes are auto-collected when you walk over them (within pickup_radius)
        # No explicit "take" needed for cubes — they're different from weapon pickups

        # ─── 3. Zone calculations ─────────────────────────────────
        zone = obs.get("zone", {})
        zc = zone.get("next_center", zone.get("center", [0.0, 0.0]))
        zr = zone.get("next_radius", zone.get("radius", 300.0))
        curr_center = zone.get("center", [0.0, 0.0])
        curr_radius = zone.get("radius", 300.0)
        zone_dps = zone.get("dps", 1.0)
        shrink_end = zone.get("shrink_end", 0.0)

        dist_to_next = dist2d(pos, zc)
        dist_to_curr = dist2d(pos, curr_center)
        to_zone_x, to_zone_y = norm2d(zc[0] - px, zc[1] - py)

        # Am I actually safe? Check both current and next zone
        outside_current = dist_to_curr > curr_radius
        will_be_outside = dist_to_next > zr

        # ─── 4. Threat assessment ─────────────────────────────────
        all_mechs = obs.get("mechs", [])
        enemy_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        npcs = [m for m in all_mechs if m.get("kind") == "npc"]
        projectiles = obs.get("projectiles", [])
        sounds = obs.get("sounds", [])
        drops = obs.get("drops", [])
        beams = obs.get("beams", [])
        crates = obs.get("crates", [])

        # Sort enemies by distance
        for e in enemy_mechs:
            e["_dist"] = dist2d(e["pos"], pos)
        enemy_mechs.sort(key=lambda m: m["_dist"])

        # Count threats at different ranges
        enemies_close = [e for e in enemy_mechs if e["_dist"] < 30]
        enemies_mid = [e for e in enemy_mechs if 30 <= e["_dist"] < 70]
        enemies_far = [e for e in enemy_mechs if e["_dist"] >= 70]

        # Sound-based threat awareness
        sound_threats_near = [s for s in sounds if s.get("dist") == "near" and s.get("kind") in ("gun", "mortar", "laser", "lightning")]
        sound_threats_mid = [s for s in sounds if s.get("dist") == "mid" and s.get("kind") in ("gun", "mortar", "laser", "lightning")]

        # ─── 5. Projectile dodge ──────────────────────────────────
        # This is the #1 survival improvement!
        dodge_x, dodge_y = 0.0, 0.0
        dodge_urgency = 0.0

        for proj in projectiles:
            pp = proj["pos"]
            pv = proj["vel"]
            pkind = proj.get("kind", "gun")

            # Vector from projectile to me
            to_me_x = px - pp[0]
            to_me_y = py - pp[1]
            to_me_z = (pz + 5.0) - pp[2]  # center-of-mass

            # Projectile speed
            proj_speed = math.sqrt(pv[0]**2 + pv[1]**2 + pv[2]**2)
            if proj_speed < 1.0:
                continue

            # Time for projectile to reach my distance
            proj_dist = math.sqrt(to_me_x**2 + to_me_y**2 + to_me_z**2)
            time_to_reach = proj_dist / proj_speed

            if time_to_reach > 1.0:
                continue  # Too far away

            # Predict projectile position at time_to_reach
            pred_x = pp[0] + pv[0] * time_to_reach
            pred_y = pp[1] + pv[1] * time_to_reach
            pred_z = pp[2] + pv[2] * time_to_reach

            # How close will it pass?
            miss_dist_h = dist2d([pred_x, pred_y], pos)
            miss_dist_v = abs(pred_z - (pz + 5.0))

            # Only dodge if it's going to come close
            if miss_dist_h < 12.0 and miss_dist_v < 10.0:
                # Dodge perpendicular to projectile velocity
                pv_nx, pv_ny = norm2d(pv[0], pv[1])
                perp_x, perp_y = rotate90(pv_nx, pv_ny, self.strafe_dir)

                # Urgency based on time and closeness
                urgency = max(0, (1.0 - time_to_reach)) * max(0, (12.0 - miss_dist_h)) / 12.0

                if pkind == "mortar":
                    urgency *= 2.0  # Mortars are more dangerous

                dodge_x += perp_x * urgency
                dodge_y += perp_y * urgency
                dodge_urgency = max(dodge_urgency, urgency)

        # Also dodge beams (laser/lightning) — they're instant but tell us where enemies are aiming
        for beam in beams:
            if beam.get("age", 1) < 0.2:  # Recent beam
                path = beam.get("path", [])
                if len(path) >= 2:
                    bp0 = path[0]
                    bp1 = path[-1]
                    # Direction of beam
                    bdx = bp1[0] - bp0[0]
                    bdy = bp1[1] - bp0[1]
                    bn_x, bn_y = norm2d(bdx, bdy)
                    # If beam passes near us, dodge perpendicular
                    to_me_from_beam = dist2d(bp0, pos)
                    if to_me_from_beam < 40:
                        perp_x, perp_y = rotate90(bn_x, bn_y, self.strafe_dir)
                        dodge_x += perp_x * 0.5
                        dodge_y += perp_y * 0.5
                        dodge_urgency = max(dodge_urgency, 0.4)

        # ─── 6. Movement planning ────────────────────────────────
        # Start with zone-directed movement as base
        move_x, move_y = to_zone_x, to_zone_y
        should_fire = False
        aim_yaw = math.atan2(to_zone_y, to_zone_x)
        aim_pitch = 0.0
        target = None
        combat_mode = False

        # Determine if we should engage or flee
        # FLEE if: low HP, multiple enemies close, or early game with enemies
        should_flee = False

        if hp_ratio < 0.25:
            should_flee = True
        elif len(enemies_close) >= 2:
            should_flee = True
        elif phase == "early" and len(enemies_close) >= 1 and cubes < 3:
            should_flee = True  # Don't fight early without upgrades

        # ─── 6a. Flee behavior ────────────────────────────────────
        if should_flee and enemy_mechs:
            # Flee away from nearest enemy, biased toward zone center
            ne = enemy_mechs[0]
            flee_x, flee_y = norm2d(px - ne["pos"][0], py - ne["pos"][1])

            # Blend flee direction with zone direction
            if outside_current or will_be_outside:
                # Zone is more important when we're outside
                move_x = flee_x * 0.3 + to_zone_x * 0.7
                move_y = flee_y * 0.3 + to_zone_y * 0.7
            else:
                move_x = flee_x * 0.6 + to_zone_x * 0.4
                move_y = flee_y * 0.6 + to_zone_y * 0.4
            move_x, move_y = norm2d(move_x, move_y)

            # Still aim at nearest enemy for defensive fire
            if ne["_dist"] < 40:
                aim_yaw, aim_pitch = self._lead_aim(ne, pos)
                should_fire = True
                combat_mode = True

        # ─── 6b. Combat behavior ─────────────────────────────────
        elif enemy_mechs and not should_flee:
            target = enemy_mechs[0]
            tdist = target["_dist"]

            # Aim with leading
            aim_yaw, aim_pitch = self._lead_aim(target, pos)
            combat_mode = True

            # Always fire when we see enemies (but see exceptions below)
            should_fire = True

            # Circle-strafe around enemy
            # Away vector from enemy
            away_x, away_y = norm2d(px - target["pos"][0], py - target["pos"][1])
            # Tangent (perpendicular) for strafing
            tang_x, tang_y = rotate90(away_x, away_y, self.strafe_dir)

            if tdist < 15:
                # Very close — back away while strafing
                move_x = away_x * 0.7 + tang_x * 0.3
                move_y = away_y * 0.7 + tang_y * 0.3
            elif tdist < 40:
                # Good combat range — strafe dominantly with slight zone pull
                move_x = tang_x * 0.6 + to_zone_x * 0.2 + away_x * 0.2
                move_y = tang_y * 0.6 + to_zone_y * 0.2 + away_y * 0.2
            elif tdist < 70:
                # Medium range — strafe with zone bias
                move_x = tang_x * 0.4 + to_zone_x * 0.4 + away_x * 0.2
                move_y = tang_y * 0.4 + to_zone_y * 0.4 + away_y * 0.2
            else:
                # Far — don't engage if we can avoid it, move toward zone
                move_x = to_zone_x * 0.7 + tang_x * 0.3
                move_y = to_zone_y * 0.7 + tang_y * 0.3
                # Only fire at long range if we have good HP
                should_fire = hp_ratio > 0.6

            move_x, move_y = norm2d(move_x, move_y)

        # ─── 6c. NPC farming ─────────────────────────────────────
        elif npcs and phase in ("early", "mid"):
            # Farm NPCs for cubes (each drops 2 cubes)
            npcs.sort(key=lambda n: dist2d(n["pos"], pos))
            npc = npcs[0]
            ndist = dist2d(npc["pos"], pos)

            if ndist < 50:
                aim_yaw, aim_pitch = self._lead_aim(npc, pos)
                should_fire = True
                combat_mode = True

                # Kite the NPC — keep moderate distance
                away_x, away_y = norm2d(px - npc["pos"][0], py - npc["pos"][1])
                tang_x, tang_y = rotate90(away_x, away_y, self.strafe_dir)

                if ndist < 15:
                    move_x = away_x * 0.6 + tang_x * 0.4
                    move_y = away_y * 0.6 + tang_y * 0.4
                elif ndist < 35:
                    move_x = tang_x * 0.5 + to_zone_x * 0.3 + away_x * 0.2
                    move_y = tang_y * 0.5 + to_zone_y * 0.3 + away_y * 0.2
                else:
                    # Approach
                    move_x = -away_x * 0.5 + tang_x * 0.3 + to_zone_x * 0.2
                    move_y = -away_y * 0.5 + tang_y * 0.3 + to_zone_y * 0.2
                move_x, move_y = norm2d(move_x, move_y)

        # ─── 6d. Supply drop pursuit ─────────────────────────────
        elif drops:
            # Find the best drop to go for
            best_drop = None
            best_drop_score = -999

            for d in drops:
                dpos = d["pos"]
                ddist = dist2d(dpos, pos)
                lands_in = d.get("lands_in", 0)
                holder = d.get("holder")
                progress = d.get("progress", 0)

                # Score: prefer close, landed, uncontested drops
                score = -ddist
                if lands_in > 0:
                    score -= lands_in * 10  # Penalize not-yet-landed
                if holder is not None and holder != me["id"]:
                    score -= 200  # Someone else is capturing
                if ddist > 100:
                    score -= 500  # Too far

                if score > best_drop_score:
                    best_drop_score = score
                    best_drop = d

            if best_drop and best_drop_score > -300:
                dp = best_drop["pos"]
                move_x, move_y = norm2d(dp[0] - px, dp[1] - py)
                aim_yaw = math.atan2(move_y, move_x)

        # ─── 6e. Loot pursuit (weapons on ground) ────────────────
        elif not weapons[1] and all_pickups:
            # We need a secondary weapon
            wpref = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 0}
            good_pickups = [p for p in all_pickups if wpref.get(p.get("kind", ""), 0) > 0]
            if good_pickups:
                good_pickups.sort(key=lambda p: dist2d(p["pos"], pos))
                best_p = good_pickups[0]
                pdist = dist2d(best_p["pos"], pos)
                if pdist < 60:
                    move_x, move_y = norm2d(best_p["pos"][0] - px, best_p["pos"][1] - py)
                    aim_yaw = math.atan2(move_y, move_x)

        # ─── 6f. Crate breaking ──────────────────────────────────
        elif crates and phase in ("early", "mid") and not enemy_mechs:
            crates_sorted = sorted(crates, key=lambda c: dist2d(c["pos"], pos))
            best_crate = crates_sorted[0]
            cdist = dist2d(best_crate["pos"], pos)
            if cdist < 40:
                cp = best_crate["pos"]
                aim_yaw = math.atan2(cp[1] - py, cp[0] - px)
                dist_h = max(1.0, dist2d(cp, pos))
                aim_pitch = math.atan2((cp[2] + 2.0) - (pz + 7.0), dist_h)
                should_fire = True
                combat_mode = True
                if cdist > self.pickup_radius + 2:
                    move_x, move_y = norm2d(cp[0] - px, cp[1] - py)

        # ─── 6g. Passive zone patrol ─────────────────────────────
        else:
            # No enemies, no targets — position smartly in zone
            # Stay at ~60-75% radius from center (edge of safe zone)
            # This keeps us away from center where bots cluster

            if dist_to_next < zr * 0.3:
                # Too central — push out toward edge
                out_x, out_y = norm2d(px - zc[0], py - zc[1])
                # Add slight tangential drift so we're not predictable
                tang_x, tang_y = rotate90(out_x, out_y, self.strafe_dir)
                move_x = out_x * 0.6 + tang_x * 0.4
                move_y = out_y * 0.6 + tang_y * 0.4
            elif dist_to_next > zr * 0.85:
                # Getting close to edge — move inward
                move_x, move_y = to_zone_x, to_zone_y
            else:
                # Sweet spot — orbit tangentially along the edge
                to_center_x, to_center_y = norm2d(zc[0] - px, zc[1] - py)
                tang_x, tang_y = rotate90(to_center_x, to_center_y, self.strafe_dir)
                move_x = tang_x * 0.8 + to_center_x * 0.2
                move_y = tang_y * 0.8 + to_center_y * 0.2

            move_x, move_y = norm2d(move_x, move_y)

        # ─── 7. Apply projectile dodge overlay ────────────────────
        if dodge_urgency > 0.1:
            dn_x, dn_y = norm2d(dodge_x, dodge_y)
            # Blend dodge into movement — higher urgency = more dodge
            blend = min(0.9, dodge_urgency)
            move_x = move_x * (1 - blend) + dn_x * blend
            move_y = move_y * (1 - blend) + dn_y * blend
            move_x, move_y = norm2d(move_x, move_y)

        # ─── 8. Zone urgency override ─────────────────────────────
        if outside_current:
            # URGENT: get back in zone
            move_x = to_zone_x * 0.85 + move_x * 0.15
            move_y = to_zone_y * 0.85 + move_y * 0.15
            move_x, move_y = norm2d(move_x, move_y)
        elif will_be_outside and match_time > shrink_end - 20:
            # Zone is about to shrink and we'll be outside
            move_x = to_zone_x * 0.7 + move_x * 0.3
            move_y = to_zone_y * 0.7 + move_y * 0.3
            move_x, move_y = norm2d(move_x, move_y)

        # ─── 9. Sound-based aim (when no visible enemies) ─────────
        if not combat_mode and sounds:
            # Face the nearest threat sound
            for s in sounds:
                if s.get("kind") in ("gun", "laser", "lightning", "mortar"):
                    b = s.get("bearing", 0)
                    if s.get("dist") == "near":
                        aim_yaw = b
                        break
                    elif s.get("dist") == "mid":
                        aim_yaw = b
                        # Don't break — keep looking for "near"

        # ─── 10. Anti-stuck logic ─────────────────────────────────
        if self.last_pos is not None:
            moved = dist2d(pos, self.last_pos)
            if moved < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        self.last_pos = list(pos)

        if self.stuck_ticks > 2:
            # Rotate movement 90° and jump
            move_x, move_y = rotate90(move_x, move_y, self.strafe_dir)
            if self.stuck_ticks > 6:
                # Try the other direction
                move_x, move_y = -move_x, -move_y
                self.strafe_dir *= -1

        # Rhythmic jumping to clear terrain curbs
        should_jump = (current_tick % 25 == 0) or (self.stuck_ticks > 1)
        if not grounded:
            should_jump = False  # Don't waste jump input while airborne

        # ─── 11. Boost logic ─────────────────────────────────────
        # Boost when: out of zone, fleeing, or energy is high and we're moving purposefully
        should_boost = False
        if not in_zone:
            should_boost = True  # Always boost to get back in zone
        elif should_flee and energy > 25:
            should_boost = True  # Boost when fleeing
        elif dodge_urgency > 0.5 and energy > 30:
            should_boost = True  # Boost to dodge
        elif energy > 70 and dist_to_next > zr * 0.6:
            should_boost = True  # Use excess energy for repositioning

        # ─── 12. Fire control ─────────────────────────────────────
        # Determine which weapon slots to fire
        fire_slot0 = False
        fire_slot1 = False

        if should_fire:
            w0 = weapons[0]  # Gun (always present)
            w1 = weapons[1]  # Secondary (may be None)

            if w0 is not None:
                # Gun: always fire if we should fire
                cd0 = w0.get("cooldown", 0)
                ammo0 = w0.get("ammo", 30)
                reload0 = w0.get("reload", 0)
                if cd0 <= 0 and ammo0 > 0 and reload0 <= 0:
                    fire_slot0 = True

                # Auto-reload when ammo is low and not in immediate danger
                if ammo0 <= 0 and not enemies_close:
                    action["reload"] = True

            if w1 is not None:
                w1_kind = w1.get("kind", "gun")
                cd1 = w1.get("cooldown", 0)

                if w1_kind == "laser":
                    # Laser: charge and fire at close-mid range
                    charge = w1.get("charge", 0)
                    # Fire when charged or close enemy
                    fire_slot1 = True
                elif w1_kind == "lightning":
                    # Lightning: fire at close range
                    if target and target["_dist"] < 50:
                        fire_slot1 = cd1 <= 0
                    elif enemies_close:
                        fire_slot1 = cd1 <= 0
                elif w1_kind == "mortar":
                    # Mortar: fire at mid-long range
                    if target and 20 < target["_dist"] < 80:
                        fire_slot1 = cd1 <= 0
                elif w1_kind == "gun":
                    fire_slot1 = cd1 <= 0

        # ─── 13. Final action assembly ────────────────────────────
        action.update(
            move=[move_x, move_y],
            aim=[aim_yaw, aim_pitch],
            boost=should_boost,
            jump=should_jump,
            fire=[fire_slot0, fire_slot1],
        )

        return action

    def _lead_aim(self, target, my_pos):
        """Compute aim yaw and pitch with iterative projectile lead prediction."""
        tpos = target["pos"]
        tvel = target.get("vel", [0, 0, 0])
        px, py, pz = my_pos

        # Use gun speed for lead calculation
        # Iterative lead: 2 iterations for better accuracy
        lead_t = 0.0
        for _ in range(2):
            # Predicted target position
            lx = tpos[0] + tvel[0] * lead_t
            ly = tpos[1] + tvel[1] * lead_t
            lz = tpos[2] + tvel[2] * lead_t

            # Distance to predicted position
            d = dist2d([lx, ly], [px, py])
            lead_t = min(0.6, d / max(1.0, self.gun_speed))

        # Final predicted position
        lx = tpos[0] + tvel[0] * lead_t
        ly = tpos[1] + tvel[1] * lead_t
        lz = tpos[2] + tvel[2] * lead_t

        # Aim at center-of-mass (mech torso height ~4.5m above feet)
        target_z = lz + 4.5
        my_z = pz + 7.0  # Our gun height

        yaw = math.atan2(ly - py, lx - px)
        hdist = max(1.0, dist2d([lx, ly], [px, py]))
        pitch = math.atan2(target_z - my_z, hdist)

        return yaw, pitch


if __name__ == "__main__":
    mf.run(PhantomBot)
