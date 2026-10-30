"""以固定 seed 呼叫 AvatarRuntime 累積 ambient_plan，輸出 [{t, plan}] JSON 供 measure.mjs 使用。

用法：
  python3 scripts/idle-motion/gen-plans.py [seed]                      # 一般 idle，累積 600 秒
  python3 scripts/idle-motion/gen-plans.py --action hum_sway [--module path/to/avatar_runtime.py]
      # 單一動作各跑 12 個 seed，輸出 [{seed, plans, end}]（--module 可載入舊版後端做對照）
"""
import argparse
import importlib.util
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

parser = argparse.ArgumentParser()
parser.add_argument("seed", nargs="?", type=int, default=7)
parser.add_argument("--action")
parser.add_argument("--module")
args = parser.parse_args()

if args.module:
    spec = importlib.util.spec_from_file_location("avatar_runtime_x", args.module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
else:
    from backend.services import avatar_runtime as mod


async def _noop(_):
    pass


if args.action:
    out = []
    for seed in range(12):
        rt = mod.AvatarRuntime(_noop, rng=random.Random(seed))
        rt._steps.extend(rt._plan_action(args.action))
        t, plans = 0.0, []
        while rt._steps:
            plan, delay = rt._next_plan()
            plans.append({"t": t, "plan": plan})
            t += delay
        out.append({"seed": seed, "plans": plans, "end": t})
else:
    rt = mod.AvatarRuntime(_noop, rng=random.Random(args.seed))
    t, out = 0.0, []
    while t < 600:
        plan, delay = rt._next_plan()
        out.append({"t": t, "plan": plan})
        t += delay
json.dump(out, sys.stdout)
