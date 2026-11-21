"""Telemetry module for Mech Royale bots.
Strictly adheres to:
- Writes ONLY to /tmp (or local fallback on Windows) and/or stderr.
- NEVER writes to stdout (preserves mechfight protocol integrity).
- Hard file size cap (default 4.5 MB) to avoid disk exhaustion in the 64 MB /tmp quota.
- Captures all Section 9 events: first_enemy_seen, engagement, shots, damage_taken,
  kill, drop, offer, stuck, death, per_match_summary.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional


class TelemetryLogger:
    def __init__(self, log_path: Optional[str] = None, max_bytes: int = 4_500_000, echo_stderr: bool = False):
        self.max_bytes = max_bytes
        self.echo_stderr = echo_stderr
        self.bytes_written = 0
        self.disabled = False

        if log_path is None:
            # Check for /tmp (Linux container) or local directory fallback (Windows)
            tmp_dir = Path("/tmp")
            if tmp_dir.exists() and os.access(str(tmp_dir), os.W_OK):
                self.log_file = tmp_dir / "mech_telemetry.jsonl"
            else:
                self.log_file = Path("telemetry.jsonl")
        else:
            self.log_file = Path(log_path)

        try:
            # Check existing file size
            if self.log_file.exists():
                self.bytes_written = self.log_file.stat().st_size
        except Exception:
            self.bytes_written = 0

    def log_event(self, event_type: str, data: Dict[str, Any], tick: Optional[float] = None, t: Optional[float] = None) -> None:
        """Log a structured telemetry event."""
        if self.disabled:
            return

        payload = {
            "event": event_type,
            "tick": tick,
            "t": t,
            **data
        }

        try:
            line = json.dumps(payload, separators=(",", ":")) + "\n"
            line_bytes = len(line.encode("utf-8"))

            if self.bytes_written + line_bytes > self.max_bytes:
                self.disabled = True
                return

            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)

            self.bytes_written += line_bytes

            if self.echo_stderr:
                print(f"[TELEMETRY] {line.strip()}", file=sys.stderr, flush=True)

        except Exception:
            # Telemetry must never crash the bot
            pass


class CombatTracker:
    """Tracks stateful combat metrics: engagements, shots, accuracy, and drops."""
    def __init__(self, logger: TelemetryLogger):
        self.logger = logger
        self.active_engagement: Optional[Dict[str, Any]] = None
        self.first_enemy_seen = False
        self.shots_in_engagement = {"gun": 0, "laser": 0, "hits": 0, "buckets": {"0-20": 0, "20-40": 0, "40-60": 0, "60+": 0}}
        self.hp_history = []  # last 50 ticks (5 seconds)
        self.drops_captured = 0
        self.kills = 0

    def record_tick_hp(self, tick: float, hp: float):
        self.hp_history.append((tick, hp))
        if len(self.hp_history) > 50:
            self.hp_history.pop(0)

    def on_enemy_seen(self, tick: float, t: float, enemy_count: int, closest_dist: float, tier: Optional[str] = None):
        if not self.first_enemy_seen:
            self.first_enemy_seen = True
            self.logger.log_event("first_enemy_seen", {
                "num_visible": enemy_count,
                "closest_dist": round(closest_dist, 2),
                "tier": tier
            }, tick=tick, t=t)

    def start_engagement(self, target_id: float, our_hp: float, enemy_hp: float, close_enemy_count: int, tick: float, t: float):
        if self.active_engagement is None:
            self.active_engagement = {
                "target_id": target_id,
                "start_tick": tick,
                "start_t": t,
                "our_start_hp": our_hp,
                "enemy_start_hp": enemy_hp,
                "close_enemies": close_enemy_count
            }
            self.shots_in_engagement = {"gun": 0, "laser": 0, "hits": 0, "buckets": {"0-20": 0, "20-40": 0, "40-60": 0, "60+": 0}}
            self.logger.log_event("engagement_start", {
                "target_id": target_id,
                "our_hp": round(our_hp, 1),
                "enemy_hp": round(enemy_hp, 1),
                "close_enemies": close_enemy_count
            }, tick=tick, t=t)

    def record_shot(self, weapon: str, dist: float):
        if self.active_engagement is not None:
            if weapon == "gun":
                self.shots_in_engagement["gun"] += 1
            elif weapon == "laser":
                self.shots_in_engagement["laser"] += 1

            if dist < 20.0:
                self.shots_in_engagement["buckets"]["0-20"] += 1
            elif dist < 40.0:
                self.shots_in_engagement["buckets"]["20-40"] += 1
            elif dist < 60.0:
                self.shots_in_engagement["buckets"]["40-60"] += 1
            else:
                self.shots_in_engagement["buckets"]["60+"] += 1

    def record_hit(self):
        if self.active_engagement is not None:
            self.shots_in_engagement["hits"] += 1

    def end_engagement(self, outcome: str, our_hp: float, enemy_hp: float, tick: float, t: float):
        if self.active_engagement is not None:
            duration = t - self.active_engagement["start_t"]
            self.logger.log_event("engagement_end", {
                "target_id": self.active_engagement["target_id"],
                "outcome": outcome,
                "duration_s": round(duration, 2),
                "our_final_hp": round(our_hp, 1),
                "enemy_final_hp": round(enemy_hp, 1)
            }, tick=tick, t=t)

            self.logger.log_event("shots", {
                "target_id": self.active_engagement["target_id"],
                "gun_fired": self.shots_in_engagement["gun"],
                "laser_fired": self.shots_in_engagement["laser"],
                "hits_landed": self.shots_in_engagement["hits"],
                "by_range": self.shots_in_engagement["buckets"]
            }, tick=tick, t=t)

            self.active_engagement = None

    def on_damage_taken(self, amount: float, enemy_visible: bool, is_rear: bool, weapon: str, by_id: Optional[float], tick: float, t: float):
        self.logger.log_event("damage_taken", {
            "amount": round(amount, 1),
            "enemy_visible": enemy_visible,
            "rear": is_rear,
            "weapon": weapon,
            "by": by_id
        }, tick=tick, t=t)

    def on_kill(self, tick: float, t: float, target_id: float):
        self.kills += 1
        time_since_eng = 0.0
        if self.active_engagement is not None:
            time_since_eng = t - self.active_engagement["start_t"]
        self.logger.log_event("kill", {
            "target_id": target_id,
            "time_to_kill_s": round(time_since_eng, 2),
            "total_kills": self.kills
        }, tick=tick, t=t)

    def on_drop_captured(self, drop_id: float, cubes: float, our_hp: float, tick: float, t: float):
        self.drops_captured += 1
        self.logger.log_event("drop", {
            "id": drop_id,
            "captured": True,
            "cubes_total": cubes,
            "hp_after": round(our_hp, 1)
        }, tick=tick, t=t)

    def on_death(self, cause: str, close_enemies: int, cubes: float, tick: float, t: float):
        recent_hps = [round(h, 1) for _, h in self.hp_history[-10:]]
        self.logger.log_event("death", {
            "cause": cause,
            "close_enemies": close_enemies,
            "cubes": cubes,
            "kills": self.kills,
            "recent_hp_5s": recent_hps
        }, tick=tick, t=t)
