"""各 idle 動作的次數、時間佔比、Z 軸幅度／移動／停留／峰速（3 個 seed、各 600 秒）。

用法：python3 scripts/idle-motion/action-stats.py
"""
import collections
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from backend.services.avatar_runtime import AvatarRuntime  # noqa: E402

SEEDS = (7, 11, 23)
HEAD_Z_DEG = 23


async def _noop(_):
    pass


def _new_stats():
    return {"n": 0, "time": 0.0, "dz": [], "move": [], "hold": [], "vz": [], "rev": 0}


stats = collections.defaultdict(_new_stats)
for seed in SEEDS:
    rt = AvatarRuntime(_noop, rng=random.Random(seed))
    choose = rt._choose_action
    current = {}

    def record(choose=choose, current=current):
        action = choose()
        current["action"] = action
        stats[action]["n"] += 1
        return action

    rt._choose_action = record
    t, prev_z, prev_dir = 0.0, 0.0, 0
    while t < 600:
        plan, delay = rt._next_plan()
        s = stats[current["action"]]
        s["time"] += delay
        z = plan["target"]["headZ"] * HEAD_Z_DEG
        dz = z - prev_z
        if abs(dz) > 0.5:
            move_ms = plan["moveMs"]
            s["dz"].append(abs(dz))
            s["move"].append(move_ms)
            s["hold"].append(delay - move_ms / 1000)
            s["vz"].append(1.875 * abs(dz) / (move_ms / 1000))
            direction = 1 if dz > 0 else -1
            if prev_dir and direction != prev_dir:
                s["rev"] += 1
            prev_dir = direction
        prev_z = z
        t += delay

total = sum(s["time"] for s in stats.values())
print(f"{'action':13} {'次/10min':>8} {'時間%':>6} {'|ΔZ|°中位':>9} {'|ΔZ|°max':>8} {'move ms':>9} {'hold s':>9} {'峰速°/s':>8} {'反向':>5}")
for action, s in sorted(stats.items(), key=lambda kv: -kv[1]["time"]):
    share = 100 * s["time"] / total
    if not s["dz"]:
        print(f"{action:13} {s['n'] / len(SEEDS):8.1f} {share:6.1f}  (無明顯 Z 變化)")
        continue
    print(f"{action:13} {s['n'] / len(SEEDS):8.1f} {share:6.1f} {st.median(s['dz']):9.1f} {max(s['dz']):8.1f} "
          f"{int(st.median(s['move'])):9d} {st.median(s['hold']):9.2f} {st.median(s['vz']):8.1f} {s['rev'] / len(SEEDS):5.0f}")
