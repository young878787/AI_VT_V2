MICRO_EVENT_LIBRARY = {
    "smirk_left": {
        "kind": "smirk_left",
        "durationMs": 520,
        "patch": {"mouthForm": 0.42, "eyeLSmile": 0.66},
        "returnToBase": True,
    },
    "surprised_pop": {
        "kind": "surprised_pop",
        "durationMs": 320,
        "patch": {"eyeLOpen": 1.14, "eyeROpen": 1.14, "browLY": 0.28, "browRY": 0.28},
        "returnToBase": True,
    },
    "smirk_right": {
        "kind": "smirk_right",
        "durationMs": 520,
        "patch": {"mouthForm": 0.42, "eyeRSmile": 0.66},
        "returnToBase": True,
    },
    "wink_left": {
        "kind": "wink_left",
        "durationMs": 240,
        "patch": {"eyeLOpen": 0.02, "blushLevel": 0.12},
        "returnToBase": True,
    },
    "wink_right": {
        "kind": "wink_right",
        "durationMs": 240,
        "patch": {"eyeROpen": 0.02, "blushLevel": 0.12},
        "returnToBase": True,
    },
    "goofy_eye_cross_bias": {
        "kind": "goofy_eye_cross_bias",
        "durationMs": 380,
        "patch": {"eyeLOpen": 0.58, "eyeROpen": 0.94, "browLAngle": 0.18, "mouthForm": 0.10},
        "returnToBase": True,
    },
    "uneven_brow_pop": {
        "kind": "uneven_brow_pop",
        "durationMs": 300,
        "patch": {"browLY": 0.28, "browRY": 0.04, "browLAngle": 0.14},
        "returnToBase": True,
    },
    "gloom_drop": {
        "kind": "gloom_drop",
        "durationMs": 500,
        "patch": {"eyeLOpen": 0.55, "eyeROpen": 0.55, "browLY": -0.16, "browRY": -0.16, "mouthForm": -0.08, "blushLevel": -0.15},
        "returnToBase": True,
    },
    "tense_squeeze": {
        "kind": "tense_squeeze",
        "durationMs": 400,
        "patch": {"eyeLOpen": 0.62, "eyeROpen": 0.62, "browLAngle": -0.12, "browRAngle": 0.12, "browLX": 0.10, "browRX": -0.10, "blushLevel": -0.08},
        "returnToBase": True,
    },
    "shock_pop": {
        "kind": "shock_pop",
        "durationMs": 200,
        "patch": {"eyeLOpen": 1.25, "eyeROpen": 1.25, "browLY": 0.32, "browRY": 0.32, "mouthForm": 0.14},
        "returnToBase": True,
    },
    "volatile_twitch": {
        "kind": "volatile_twitch",
        "durationMs": 280,
        "patch": {"eyeLOpen": 0.60, "eyeROpen": 1.02, "browLAngle": 0.10, "browRAngle": -0.14, "mouthForm": 0.06},
        "returnToBase": True,
    },
    "meltdown_warp": {
        "kind": "meltdown_warp",
        "durationMs": 450,
        "patch": {"eyeLOpen": 0.40, "eyeROpen": 1.15, "browLAngle": 0.35, "browRAngle": 0.10, "mouthForm": -0.12, "browLForm": -0.20, "blushLevel": 0.08},
        "returnToBase": True,
    },
    "awkward_freeze": {
        "kind": "awkward_freeze",
        "durationMs": 600,
        "patch": {"eyeLOpen": 0.90, "eyeROpen": 1.0, "browLY": 0.06, "browRY": 0.10, "mouthForm": 0.02, "blushLevel": 0.10},
        "returnToBase": True,
    },
}


SEQUENCE_LIBRARY = {
    "bright_talk_bounce": [
        {"kind": "smirk_left", "durationMs": 400, "patch": {"mouthForm": 0.30, "eyeLSmile": 0.45}, "returnToBase": True},
        {"kind": "smirk_right", "durationMs": 400, "patch": {"mouthForm": 0.30, "eyeRSmile": 0.45}, "returnToBase": True},
    ],
    "pause_then_goofy": [
        {"kind": "awkward_freeze", "durationMs": 300, "patch": {"mouthForm": 0.02, "eyeLOpen": 0.88, "eyeROpen": 0.92, "blushLevel": 0.10}, "returnToBase": True},
        {"kind": "goofy_eye_cross_bias", "durationMs": 380, "patch": {"eyeLOpen": 0.58, "eyeROpen": 0.94, "browLAngle": 0.18}, "returnToBase": True},
    ],
    "smirk_then_flat": [
        {"kind": "smirk_left", "durationMs": 420, "patch": {"mouthForm": 0.38, "eyeLSmile": 0.55}, "returnToBase": True},
        {"kind": "awkward_freeze", "durationMs": 250, "patch": {"mouthForm": 0.04, "eyeLSmile": 0.10, "eyeRSmile": 0.10, "blushLevel": 0.10}, "returnToBase": True},
    ],
    "drop_then_gloom": [
        {"kind": "shock_pop", "durationMs": 180, "patch": {"eyeLOpen": 1.10, "eyeROpen": 1.10, "browLY": 0.20, "browRY": 0.20}, "returnToBase": True},
        {"kind": "gloom_drop", "durationMs": 500, "patch": {"eyeLOpen": 0.55, "eyeROpen": 0.55, "browLY": -0.16, "browRY": -0.16, "mouthForm": -0.08, "blushLevel": -0.15}, "returnToBase": True},
    ],
    "tense_then_break": [
        {"kind": "tense_squeeze", "durationMs": 400, "patch": {"eyeLOpen": 0.62, "eyeROpen": 0.62, "browLAngle": -0.12, "browRAngle": 0.12, "browLX": 0.10, "browRX": -0.10, "blushLevel": -0.08}, "returnToBase": True},
        {"kind": "shock_pop", "durationMs": 180, "patch": {"eyeLOpen": 1.15, "eyeROpen": 1.15}, "returnToBase": True},
    ],
    "burst_then_unstable": [
        {"kind": "shock_pop", "durationMs": 200, "patch": {"eyeLOpen": 1.25, "eyeROpen": 1.25, "browLY": 0.32, "browRY": 0.32, "mouthForm": 0.14}, "returnToBase": True},
        {"kind": "volatile_twitch", "durationMs": 280, "patch": {"eyeLOpen": 0.60, "eyeROpen": 1.02, "browLAngle": 0.10, "browRAngle": -0.14, "mouthForm": 0.06}, "returnToBase": True},
    ],
}
