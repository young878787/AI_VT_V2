"""Static rule tables used by the expression compiler."""

EMOTION_PERFORMANCE_PRESET_MAP = {
    ("happy", "smile"): "happy_smile_soft",
    ("happy", "bright_talk"): "happy_bright_talk",
    ("playful", "smile"): "happy_smile_soft",
    ("playful", "bright_talk"): "happy_bright_talk",
    ("playful", "goofy_face"): "playful_goofy_face",
    ("playful", "cheeky_wink"): "teasing_cheeky_wink",
    ("teasing", "smile"): "teasing_smug",
    ("teasing", "cheeky_wink"): "teasing_cheeky_wink",
    ("teasing", "smug"): "teasing_smug",
    ("teasing", "bright_talk"): "teasing_smug",
    ("gloomy", "gloomy"): "gloomy_deadpan",
    ("gloomy", "deadpan"): "gloomy_deadpan",
    ("gloomy", "awkward"): "gloomy_deadpan",
    ("sad", "sad"): "sad_tense_hold",
    ("sad", "tense_hold"): "sad_tense_hold",
    ("sad", "awkward"): "awkward_stuck",
    ("sad", "gloomy"): "gloomy_deadpan",
    ("angry", "angry"): "angry_meltdown",
    ("angry", "meltdown"): "angry_meltdown",
    ("angry", "volatile"): "conflicted_volatile",
    ("angry", "deadpan"): "gloomy_deadpan",
    ("conflicted", "volatile"): "conflicted_volatile",
    ("conflicted", "meltdown"): "angry_meltdown",
    ("conflicted", "awkward"): "awkward_stuck",
    ("shy", "awkward"): "awkward_stuck",
    ("shy", "shy"): "shy_tucked",
    ("surprised", "shock_recoil"): "surprised_open",
    ("surprised", "bright_talk"): "surprised_open",
    ("neutral", "smile"): "calm_soft",
    ("neutral", "deadpan"): "gloomy_deadpan",
    ("neutral", "bright_talk"): "happy_bright_talk",
    ("neutral", "awkward"): "awkward_stuck",
}


TOPIC_GUARD_RULES = {
    "crying": {
        "forbid_modes": {"goofy_face", "bright_talk", "cheeky_wink"},
        "downgrade_map": {
            "goofy_face": "awkward",
            "bright_talk": "awkward",
            "cheeky_wink": "awkward",
            "smug": "tense_hold",
        },
    },
    "gloomy": {
        "forbid_modes": {"goofy_face", "bright_talk", "smile", "cheeky_wink"},
        "downgrade_map": {
            "goofy_face": "deadpan",
            "bright_talk": "deadpan",
            "smile": "deadpan",
            "cheeky_wink": "deadpan",
            "smug": "gloomy",
        },
    },
    "serious_argument": {
        "forbid_modes": {"goofy_face", "cheeky_wink"},
        "downgrade_map": {
            "goofy_face": "deadpan",
            "cheeky_wink": "smug",
            "bright_talk": "smile",
        },
    },
}


SIGNATURE_TO_PRESET = {
    "happy_soft": "happy_smile_soft",
    "bright_talk": "happy_bright_talk",
    "goofy_asym": "playful_goofy_face",
    "cheeky_wink": "teasing_cheeky_wink",
    "smug_tease": "teasing_smug",
    "gloomy_deadpan": "gloomy_deadpan",
    "sad_tense": "sad_tense_hold",
    "angry_meltdown": "angry_meltdown",
    "volatile_unstable": "conflicted_volatile",
    "awkward_stuck": "awkward_stuck",
    "surprised_shock": "surprised_open",
    "shy_tucked": "shy_tucked",
    "calm_soft": "calm_soft",
}


CONTINUITY_FAMILY_MAP = {
    "happy": "warm",
    "playful": "warm",
    "teasing": "warm",
    "angry": "tension",
    "conflicted": "tension",
    "sad": "low",
    "gloomy": "low",
    "shy": "shy",
    "surprised": "reactive",
    "neutral": "neutral",
}


CONTINUITY_PARAM_WEIGHTS = {
    "headIntensity": 0.25,
    "bodyAngleX": 0.22,
    "bodyAngleY": 0.20,
    "bodyAngleZ": 0.20,
    "breathLevel": 0.30,
    "physicsImpulse": 0.18,
    "blushLevel": 0.65,
    "eyeLOpen": 0.38,
    "eyeROpen": 0.38,
    "mouthForm": 0.55,
    "browLY": 0.45,
    "browRY": 0.45,
    "browLAngle": 0.48,
    "browRAngle": 0.48,
    "browLForm": 0.52,
    "browRForm": 0.52,
    "eyeLSmile": 0.5,
    "eyeRSmile": 0.5,
    "browLX": 0.42,
    "browRX": 0.42,
}


MOTION_PARAM_DEFAULTS = {
    "bodyAngleX": 0.0,
    "bodyAngleY": 0.0,
    "bodyAngleZ": 0.0,
    "breathLevel": 0.35,
    "physicsImpulse": 0.12,
}
