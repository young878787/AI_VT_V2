def render_legacy_behavior_payload(plan: dict) -> dict:
    params = plan["basePose"]["params"]
    behavior_payload = {
        "type": "behavior",
        "headIntensity": params["headIntensity"],
        "blushLevel": params["blushLevel"],
        "eyeSync": params["eyeSync"],
        "eyeLOpen": params["eyeLOpen"],
        "eyeROpen": params["eyeROpen"],
        "durationSec": plan["basePose"]["durationSec"],
        "mouthForm": params["mouthForm"],
        "browLY": params["browLY"],
        "browRY": params["browRY"],
        "browLAngle": params["browLAngle"],
        "browRAngle": params["browRAngle"],
        "browLForm": params["browLForm"],
        "browRForm": params["browRForm"],
        "eyeLSmile": params["eyeLSmile"],
        "eyeRSmile": params["eyeRSmile"],
        "browLX": params["browLX"],
        "browRX": params["browRX"],
    }
    return {
        "behavior_payload": behavior_payload,
        "blink_payloads": [
            {"type": "blink_control", **command}
            for command in plan.get("blinkPlan", {}).get("commands", [])
        ],
        "speaking_rate": float(plan.get("speakingRate", 1.0)),
    }
