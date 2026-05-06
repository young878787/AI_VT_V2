"""Motion-plan branches used by the expression compiler."""

from copy import deepcopy
import random


MOTION_BRANCH_LIBRARY = {
    "happy_bright_talk": [
        {
            "variant": "buoyant_bounce",
            "durationMs": 4600,
            "blendInMs": 420,
            "blendOutMs": 820,
            "body": {"sway": 1.14, "bob": 1.62, "twist": 0.92, "spring": 0.72},
            "head": {"yaw": 0.92, "pitch": 1.18, "roll": 1.06, "lagMs": 110},
        },
        {
            "variant": "side_sway_bounce",
            "durationMs": 5200,
            "blendInMs": 480,
            "blendOutMs": 900,
            "body": {"sway": 1.46, "bob": 1.32, "twist": 1.08, "spring": 0.48},
            "head": {"yaw": 1.12, "pitch": 0.92, "roll": 1.22, "lagMs": 150},
        },
        {
            "variant": "lean_in_pop",
            "durationMs": 4200,
            "blendInMs": 360,
            "blendOutMs": 760,
            "body": {"sway": 0.94, "bob": 1.48, "twist": 0.82, "spring": 0.9},
            "head": {"yaw": 0.84, "pitch": 1.38, "roll": 0.86, "lagMs": 90},
        },
    ],
    "playful_tease": [
        {
            "variant": "swing_tease",
            "durationMs": 5600,
            "blendInMs": 460,
            "blendOutMs": 880,
            "body": {"sway": 1.58, "bob": 1.16, "twist": 1.34, "spring": 0.5},
            "head": {"yaw": 1.18, "pitch": 0.76, "roll": 1.42, "lagMs": 170},
        },
        {
            "variant": "peek_shift",
            "durationMs": 4800,
            "blendInMs": 430,
            "blendOutMs": 840,
            "body": {"sway": 1.28, "bob": 1.08, "twist": 1.46, "spring": 0.38},
            "head": {"yaw": 1.36, "pitch": 0.74, "roll": 1.18, "lagMs": 190},
        },
    ],
    "angry_tension": [
        {
            "variant": "locked_glare",
            "durationMs": 5200,
            "blendInMs": 280,
            "blendOutMs": 720,
            "body": {"sway": 0.58, "bob": 0.72, "twist": 1.54, "spring": 0.16},
            "head": {"yaw": 0.64, "pitch": 0.62, "roll": 0.82, "lagMs": 40},
        },
        {
            "variant": "sharp_twist_hold",
            "durationMs": 4400,
            "blendInMs": 180,
            "blendOutMs": 680,
            "body": {"sway": 0.68, "bob": 0.82, "twist": 1.82, "spring": 0.34},
            "head": {"yaw": 0.82, "pitch": 0.58, "roll": 1.04, "lagMs": 35},
        },
    ],
    "shy_tucked": [
        {
            "variant": "tuck_side_sway",
            "durationMs": 5400,
            "blendInMs": 520,
            "blendOutMs": 920,
            "body": {"sway": 1.02, "bob": 1.24, "twist": 1.08, "spring": 0.34},
            "head": {"yaw": 0.86, "pitch": 0.92, "roll": 1.22, "lagMs": 170},
        },
        {
            "variant": "peek_return",
            "durationMs": 4600,
            "blendInMs": 440,
            "blendOutMs": 820,
            "body": {"sway": 1.2, "bob": 1.02, "twist": 1.24, "spring": 0.42},
            "head": {"yaw": 1.28, "pitch": 0.78, "roll": 1.02, "lagMs": 210},
        },
    ],
    "low_mood": [
        {
            "variant": "slow_sink",
            "durationMs": 6200,
            "blendInMs": 680,
            "blendOutMs": 1000,
            "body": {"sway": 0.42, "bob": 1.36, "twist": 0.48, "spring": 0.12},
            "head": {"yaw": 0.42, "pitch": 0.72, "roll": 0.58, "lagMs": 130},
        },
        {
            "variant": "small_recover_bob",
            "durationMs": 5200,
            "blendInMs": 560,
            "blendOutMs": 900,
            "body": {"sway": 0.54, "bob": 1.58, "twist": 0.54, "spring": 0.24},
            "head": {"yaw": 0.48, "pitch": 0.88, "roll": 0.62, "lagMs": 160},
        },
    ],
    "surprised_recoil": [
        {
            "variant": "recoil_spring",
            "durationMs": 3400,
            "blendInMs": 160,
            "blendOutMs": 760,
            "body": {"sway": 1.08, "bob": 1.88, "twist": 1.08, "spring": 1.0},
            "head": {"yaw": 0.92, "pitch": 1.56, "roll": 0.94, "lagMs": 55},
        },
    ],
    "uneasy_shift": [
        {
            "variant": "uneasy_counter_sway",
            "durationMs": 5600,
            "blendInMs": 460,
            "blendOutMs": 900,
            "body": {"sway": 1.18, "bob": 1.14, "twist": 1.38, "spring": 0.3},
            "head": {"yaw": 1.12, "pitch": 0.78, "roll": 1.18, "lagMs": 140},
        },
    ],
}


def resolve_motion_theme(emotion: str, performance_mode: str, intent: dict) -> str:
    requested = intent.get("motion_theme")
    if requested in MOTION_BRANCH_LIBRARY:
        return requested
    if performance_mode in {"bright_talk", "smile"} and emotion in {"happy", "neutral"}:
        return "happy_bright_talk"
    if emotion in {"playful", "teasing"} or performance_mode in {"goofy_face", "smug", "cheeky_wink"}:
        return "playful_tease"
    if emotion == "angry" or performance_mode in {"meltdown", "volatile"}:
        return "angry_tension"
    if emotion == "shy" or performance_mode == "awkward":
        return "shy_tucked"
    if emotion in {"sad", "gloomy"} or performance_mode in {"gloomy", "deadpan", "tense_hold"}:
        return "low_mood"
    if emotion == "surprised" or performance_mode == "shock_recoil":
        return "surprised_recoil"
    if emotion == "conflicted":
        return "uneasy_shift"
    return "happy_bright_talk"


def _choose_variant(theme: str, intent: dict, previous_state: dict | None) -> dict:
    branches = MOTION_BRANCH_LIBRARY[theme]
    requested = intent.get("motion_variant")
    if isinstance(requested, str):
        for branch in branches:
            if branch["variant"] == requested:
                return branch

    previous_variant = previous_state.get("motionVariant") if isinstance(previous_state, dict) else None
    available = [branch for branch in branches if branch["variant"] != previous_variant] or branches
    return random.choice(available)


def build_motion_plan(
    emotion: str,
    performance_mode: str,
    intensity: float,
    energy: float,
    playfulness: float,
    intent: dict,
    previous_state: dict | None,
) -> dict:
    theme = resolve_motion_theme(emotion, performance_mode, intent)
    branch = deepcopy(_choose_variant(theme, intent, previous_state))

    energy_scale = 0.92 + (energy * 0.16)
    intensity_scale = 0.94 + (intensity * 0.12)
    playfulness_scale = 0.96 + (playfulness * 0.10)
    branch["durationMs"] = int(branch["durationMs"] * (0.96 + (intensity * 0.08)))
    branch["phaseSeed"] = round(random.uniform(0.0, 6.283), 3)
    branch["theme"] = theme
    branch["body"] = {
        "sway": round(branch["body"]["sway"] * playfulness_scale, 3),
        "bob": round(branch["body"]["bob"] * energy_scale, 3),
        "twist": round(branch["body"]["twist"] * intensity_scale, 3),
        "spring": round(branch["body"]["spring"], 3),
    }
    branch["head"] = {
        "yaw": round(branch["head"]["yaw"] * intensity_scale, 3),
        "pitch": round(branch["head"]["pitch"] * energy_scale, 3),
        "roll": round(branch["head"]["roll"] * playfulness_scale, 3),
        "lagMs": branch["head"]["lagMs"],
    }
    return branch
