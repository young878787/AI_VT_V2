"""RushiaHD.physics3.json 的 idle 調校（模型資源不在 Git，這支腳本是可重現的來源）。

  - 長髮／側髮／後髮／瀏海／右緞帶補上 AngleY（Type=Angle、Weight=25）：原本只有 HairPitch 吃 Y。
    Type=Y（平移）實測對輸出角度沒有影響，所以用 Angle。
  - 各鏈第二段以後 Mobility +0.04、Delay -0.12：頭停下後更有餘擺與回彈（idle 軌跡下輸出幅度約 ×2）。
  - 輸出 Scale ×1.5。

用法：python3 scripts/idle-motion/patch-physics.py [physics3.json]
  第一次執行會把原檔備份成 <檔名>.orig，之後一律從 .orig 重新套用，可重複執行。
  還原：把 .orig 複製回原檔名。
"""
import json
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parents[2] / "public/Resources/Rushia/RushiaHD.physics3.json")
backup = path.with_name(path.name + ".orig")
if not backup.exists():
    shutil.copyfile(path, backup)

CHAINS = ("HDHairLongR", "HDHairLongL", "HDHairSideR", "HDHairSideL", "HDHairBack", "HDHairBangs", "HDRibbonR")
ANGLE_Y = {"Source": {"Target": "Parameter", "Id": "ParamAngleY"}, "Weight": 25, "Type": "Angle", "Reflect": False}
MOBILITY_DELTA, DELAY_DELTA, MAX_MOBILITY, MIN_DELAY, OUTPUT_SCALE = 0.04, -0.12, 0.99, 0.3, 1.5

data = json.loads(backup.read_text(encoding="utf-8"))
for setting in data["PhysicsSettings"]:
    if setting["Id"] not in CHAINS:
        continue
    setting["Input"].append(json.loads(json.dumps(ANGLE_Y)))
    for vertex in setting["Vertices"][1:]:
        vertex["Mobility"] = round(min(MAX_MOBILITY, vertex["Mobility"] + MOBILITY_DELTA), 4)
        vertex["Delay"] = round(max(MIN_DELAY, vertex["Delay"] + DELAY_DELTA), 4)
    for output in setting["Output"]:
        output["Scale"] = round(output["Scale"] * OUTPUT_SCALE, 4)
data["Meta"]["TotalInputCount"] = sum(len(s["Input"]) for s in data["PhysicsSettings"])
path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"patched {path} (backup: {backup.name}), inputs={data['Meta']['TotalInputCount']}")
