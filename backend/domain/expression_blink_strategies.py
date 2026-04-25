BLINK_STRATEGIES = {
    "normal": [],
    "teasing_pause": [
        {"action": "pause", "durationSec": 1.0},
        {"action": "force_blink"},
    ],
    "shy_fast": [
        {"action": "set_interval", "intervalMin": 0.8, "intervalMax": 1.5},
    ],
    "surprised_hold": [
        {"action": "pause", "durationSec": 1.2},
        {"action": "force_blink"},
    ],
    "focused_pause": [
        {"action": "pause", "durationSec": 2.0},
    ],
    "sleepy_slow": [
        {"action": "set_interval", "intervalMin": 2.0, "intervalMax": 5.0},
    ],
}
