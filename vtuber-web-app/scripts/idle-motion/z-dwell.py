"""headZ 折返停留、折返間隔、速度 p95／max、歪頭持住速度。

用法：先 `DUMP=rows.json node scripts/idle-motion/measure.mjs plans.json`，
再 `python3 scripts/idle-motion/z-dwell.py rows.json [...]`（rows 為 30Hz 輸出）。
"""
import json
import statistics as st
import sys

FPS = 30


def analyze(path):
    with open(path) as f:
        z = [row["headZ"] for row in json.load(f)]
    v = [(z[i + 1] - z[i - 1]) * FPS / 2 for i in range(1, len(z) - 1)]
    turns = [i for i in range(1, len(v)) if v[i - 1] * v[i] < 0]
    big = []
    for k in range(1, len(turns) - 1):
        i = turns[k]
        amp = min(abs(z[i] - z[turns[k - 1]]), abs(z[turns[k + 1]] - z[i]))
        if amp < 5:
            continue
        a = i
        while a > 0 and abs(v[a]) < 3:
            a -= 1
        b = i
        while b < len(v) - 1 and abs(v[b]) < 3:
            b += 1
        big.append(((b - a) / FPS, (turns[k + 1] - i) / FPS))
    held = [abs(v[i]) for i in range(1, len(v)) if abs(z[i]) > 8 and abs(v[i]) < 4]
    held_count = len([i for i in range(1, len(v)) if abs(z[i]) > 8])
    speeds = sorted(abs(x) for x in v)
    return {
        "n": len(big),
        "dwell": st.median([d for d, _ in big]),
        "gap": st.median([g for _, g in big]),
        "p95": speeds[int(len(speeds) * .95)],
        "vmax": speeds[-1],
        "held_s": held_count / FPS,
        "held_speed": st.median(held) if held else 0,
    }


for path in sys.argv[1:]:
    a = analyze(path)
    print(f"  {path.split('/')[-1]:16} 大擺幅折返 {a['n']:3d} | 折返停留(<3°/s) 中位 {a['dwell']:.2f}s | "
          f"折返間隔 中位 {a['gap']:.2f}s | Z速度 p95 {a['p95']:.1f} max {a['vmax']:.0f}°/s | "
          f"歪頭持住 {a['held_s']:.0f}s 速度中位 {a['held_speed']:.2f}°/s")
