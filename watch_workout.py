"""
Compact workout code ⇄ Garmin structured workout.

The plan message carries the workout in its "⌚ На часы" button, and Telegram
limits button data to 64 bytes, so the workout is a short code, e.g.

    w10e|4x8t/2e|c10e   = warmup 10 min easy, 4 × (8 min threshold / 2 min easy), cooldown 10 min easy

Blocks are separated by "|". Each block:
    [w|c]        optional: warmup / cooldown (a plain block is main work)
    [Nx]         optional: repeats
    DUR ZONE     minutes, or seconds with "s" (20s); zone letter
    [/DUR ZONE]  optional: recovery between repeats
Zones: r recovery, e easy, m moderate (steady; not "s", which means seconds), t threshold, v VO2max (HR ranges come
from the athlete's zones when the workout is built).
"""

import re
from dataclasses import dataclass

from garminconnect.workout import (
    ConditionType, ExecutableStep, RepeatGroup, RunningWorkout, StepType, TargetType, WorkoutSegment,
)

MAX_CODE_LEN = 60          # "W:" prefix + code must fit Telegram's 64-byte callback_data
MAX_BLOCKS = 8
NO_TARGET_BELOW_S = 60     # HR lags: strides etc. get no HR target

BLOCK = re.compile(
    r"(?P<kind>[wc]?)(?:(?P<reps>\d{1,2})x)?(?P<dur>\d{1,3}s?)(?P<zone>[remtv])"
    r"(?:/(?P<rdur>\d{1,3}s?)(?P<rzone>[remt]))?"
)
ZONE_RU = {"r": "восстановление", "e": "легко", "m": "средне", "t": "ПАНО", "v": "МПК"}


@dataclass
class Block:
    kind: str           # "warmup" | "main" | "cooldown"
    reps: int
    seconds: int
    zone: str
    rest_seconds: int = 0
    rest_zone: str = ""

    @property
    def total(self) -> int:
        return self.reps * (self.seconds + self.rest_seconds)


def _seconds(token: str) -> int:
    return int(token[:-1]) if token.endswith("s") else int(token) * 60


def parse(code: str) -> list[Block]:
    """Raises ValueError for anything that is not a sane running workout."""
    code = code.strip()
    if not code or len(code) > MAX_CODE_LEN:
        raise ValueError(f"код пустой или длиннее {MAX_CODE_LEN} символов")
    parts = code.split("|")
    if len(parts) > MAX_BLOCKS:
        raise ValueError(f"больше {MAX_BLOCKS} блоков")
    blocks = []
    for part in parts:
        m = BLOCK.fullmatch(part)
        if not m:
            raise ValueError(f"не понимаю блок «{part}»: формат [w|c][Nx]ДЛИТЕЛЬНОСТЬзона[/ДЛИТЕЛЬНОСТЬзона], "
                             f"после длительности всегда буква зоны r/e/m/t/v (8 минут ПАНО — 8t, 20 секунд МПК — 20sv)")
        kind = {"w": "warmup", "c": "cooldown", "": "main"}[m["kind"]]
        reps = int(m["reps"] or 1)
        if kind != "main" and (reps > 1 or m["rdur"]):
            raise ValueError(f"разминка/заминка без повторов: «{part}»")
        if m["rdur"] and reps == 1:
            raise ValueError(f"отдых без повторов: «{part}»")
        b = Block(kind, reps, _seconds(m["dur"]), m["zone"],
                  _seconds(m["rdur"]) if m["rdur"] else 0, m["rzone"] or "")
        if not (10 <= b.seconds <= 4 * 3600) or not (1 <= reps <= 30):
            raise ValueError(f"странная длительность или число повторов: «{part}»")
        blocks.append(b)
    total = sum(b.total for b in blocks)
    if not (10 * 60 <= total <= 5 * 3600):
        raise ValueError(f"общая длительность {total // 60} мин вне 10–300 мин")
    return blocks


def _dur_ru(seconds: int) -> str:
    return f"{seconds} с" if seconds < 60 or seconds % 60 else f"{seconds // 60} мин"


def describe(blocks: list[Block]) -> str:
    out = []
    for b in blocks:
        work = f"{_dur_ru(b.seconds)} {ZONE_RU[b.zone]}"
        if b.reps > 1:
            rest = f" / {_dur_ru(b.rest_seconds)} {ZONE_RU[b.rest_zone]}" if b.rest_seconds else ""
            work = f"{b.reps} × ({work}{rest})"
        out.append({"warmup": "разминка ", "cooldown": "заминка ", "main": ""}[b.kind] + work)
    return " → ".join(out) + f" · всего ≈{sum(b.total for b in blocks) // 60} мин"


def describe_lines(blocks: list[Block], z: dict) -> list[str]:
    """One line per block with HR ranges — the workout as it goes to the watch."""
    def part(seconds: int, zone: str) -> str:
        if seconds < NO_TARGET_BELOW_S:
            return f"{_dur_ru(seconds)} ускорение"
        lo, hi = hr_range(zone, z)
        return f"{_dur_ru(seconds)} {ZONE_RU[zone]} {lo}–{hi}"
    lines = []
    for b in blocks:
        prefix = {"warmup": "разминка ", "cooldown": "заминка ", "main": ""}[b.kind]
        if b.reps > 1:
            rest = f" / {_dur_ru(b.rest_seconds)} {ZONE_RU[b.rest_zone]}" if b.rest_seconds else ""
            lines.append(f"{prefix}{b.reps} × {part(b.seconds, b.zone)}{rest}")
        else:
            lines.append(prefix + part(b.seconds, b.zone))
    return lines


def short_title(blocks: list[Block]) -> str:
    main = [b for b in blocks if b.kind == "main"] or blocks
    key = max(main, key=lambda b: "remtv".index(b.zone))
    work = f"{key.reps}×{_dur_ru(key.seconds)}" if key.reps > 1 else _dur_ru(key.seconds)
    return f"{ZONE_RU[key.zone]} {work}"


# ── Garmin ───────────────────────────────────────────────────────────────────

def hr_range(zone: str, z: dict) -> tuple[int, int]:
    return {
        "r": (z["recovery_max"] - 25, z["recovery_max"]),
        "e": z["easy"],
        "m": z["steady"],
        "t": z["threshold"],
        "v": (z["vo2_min"], z["vo2_min"] + 15),
    }[zone]


def _step(order: int, kind: str, seconds: int, zone: str, z: dict) -> ExecutableStep:
    step_type = {"warmup": (StepType.WARMUP, "warmup", 1), "cooldown": (StepType.COOLDOWN, "cooldown", 2),
                 "interval": (StepType.INTERVAL, "interval", 3), "recovery": (StepType.RECOVERY, "recovery", 4)}[kind]
    target = {}
    if seconds >= NO_TARGET_BELOW_S:
        lo, hi = hr_range(zone, z)
        target = {"targetType": {"workoutTargetTypeId": TargetType.HEART_RATE_ZONE,
                                 "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
                  "targetValueOne": lo, "targetValueTwo": hi}
    else:
        target = {"targetType": {"workoutTargetTypeId": TargetType.NO_TARGET,
                                 "workoutTargetTypeKey": "no.target", "displayOrder": 1}}
    return ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": step_type[0], "stepTypeKey": step_type[1], "displayOrder": step_type[2]},
        endCondition={"conditionTypeId": ConditionType.TIME, "conditionTypeKey": "time",
                      "displayOrder": 2, "displayable": True},
        endConditionValue=seconds,
        **target,
    )


def to_garmin(blocks: list[Block], z: dict, name: str) -> RunningWorkout:
    steps, order = [], 0
    for b in blocks:
        if b.reps == 1:
            order += 1
            kind = b.kind if b.kind != "main" else "interval"
            steps.append(_step(order, kind, b.seconds, b.zone, z))
            continue
        order += 1
        group_order = order
        inner = []
        order += 1
        inner.append(_step(order, "interval", b.seconds, b.zone, z))
        if b.rest_seconds:
            order += 1
            inner.append(_step(order, "recovery", b.rest_seconds, b.rest_zone, z))
        steps.append(RepeatGroup(
            stepOrder=group_order,
            stepType={"stepTypeId": StepType.REPEAT, "stepTypeKey": "repeat", "displayOrder": 6},
            numberOfIterations=b.reps,
            workoutSteps=inner,
            endCondition={"conditionTypeId": ConditionType.ITERATIONS, "conditionTypeKey": "iterations",
                          "displayOrder": 7, "displayable": False},
            endConditionValue=b.reps,
            smartRepeat=False,
        ))
    return RunningWorkout(
        workoutName=name,
        estimatedDurationInSecs=sum(b.total for b in blocks),
        workoutSegments=[WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
            workoutSteps=steps,
        )],
    )
