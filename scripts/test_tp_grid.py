"""TP>5 hypothesis: sweep TP at stop=1.5 + rolling 3y walk-forward (train Y-3..Y-1, test Y)."""
import sys, json, time
sys.path.insert(0, '.')
import scripts.stress_test_2026 as st
from multiprocessing import Pool
TPS = ["4.0", "5.0", "6.0", "7.0", "8.0"]
BE = ["BTCUSDT", "ETHUSDT"]
for y in range(2020, 2027):
    syms = st.SYMBOLS if y - 3 >= 2021 else BE
    st.WINDOWS[f"tr{y}"] = (f"{y-3}-01-01", f"{y-1}-12-31 23:59", syms)
windows = ["full2"] + [f"y{y}" for y in range(2018, 2027)] + [f"tr{y}" for y in range(2020, 2027)]
jobs = [{"label": f"tp{tp}", "window": w, "overrides": {"ema_tp_mult": tp}, "meta": {"window": w, "tp": tp}}
        for w in windows for tp in TPS]
jobs.sort(key=lambda j: 0 if j["window"] == "full2" else (1 if j["window"].startswith("tr") else 2))
t = time.time()
with Pool(4, initializer=st._init_worker) as p:
    raw = p.map(st._execute, jobs, chunksize=1)
out = [{**r, "window": j["meta"]["window"], "tp": j["meta"]["tp"]} for j, r in zip(jobs, raw)]
json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)
print("done", round(time.time() - t), "s")
