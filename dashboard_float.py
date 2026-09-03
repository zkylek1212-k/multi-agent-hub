"""桌面懸浮儀表板（Tkinter，always-on-top）。

由 mcp_worker_hub.py 的 open_dashboard() 以子進程彈出：
    python dashboard_float.py <port>
每 2 秒 fetch http://127.0.0.1:<port>/api，顯示：
  - agent-hub 自身執行狀態（啟用 workers、uptime、running/done/failed、最近工具）
  - 每個 job 的派工狀態（worker、描述、狀態、耗時、當下 log 尾行）

純 stdlib，無第三方相依。fetch 失敗時顯示「等待 hub」，不會崩。
"""
import json
import sys
import tkinter as tk
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8787"
API = f"http://127.0.0.1:{PORT}/api"

BG, CARD, FG, MUT, LINE = "#0f1115", "#1a1e27", "#e6e6e6", "#8b93a7", "#2a2f3a"
RUN, OK, BAD = "#f0b429", "#3ecf8e", "#ff6b6b"
FONT = ("Microsoft JhengHei", 9)
FONT_B = ("Microsoft JhengHei", 10, "bold")
FONT_S = ("Consolas", 8)


def fmt(secs):
    return f"{secs // 60}m{secs % 60:02d}s"


def fetch():
    try:
        with urllib.request.urlopen(API, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:
        return None, e


root = tk.Tk()
root.title("🚀 派工儀表板")
root.configure(bg=BG)
root.attributes("-topmost", True)
root.geometry("380x560+40+80")
root.minsize(300, 300)

# --- 頂部：hub 自身狀態 ---
head = tk.Frame(root, bg=BG)
head.pack(fill="x", padx=10, pady=(10, 6))
hub_line1 = tk.Label(head, text="連線中…", bg=BG, fg=FG, font=FONT_B, anchor="w")
hub_line1.pack(fill="x")
hub_line2 = tk.Label(head, text="", bg=BG, fg=MUT, font=FONT, anchor="w", justify="left")
hub_line2.pack(fill="x")
tk.Frame(root, bg=LINE, height=1).pack(fill="x", padx=10)

# --- 中段：可捲動的 job 卡片 ---
mid = tk.Frame(root, bg=BG)
mid.pack(fill="both", expand=True, padx=6, pady=6)
canvas = tk.Canvas(mid, bg=BG, highlightthickness=0)
sb = tk.Scrollbar(mid, orient="vertical", command=canvas.yview)
body = tk.Frame(canvas, bg=BG)
body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
canvas.create_window((0, 0), window=body, anchor="nw", width=352)
canvas.configure(yscrollcommand=sb.set)
canvas.pack(side="left", fill="both", expand=True)
sb.pack(side="right", fill="y")
root.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

# --- 底部：hub 最近事件 ---
tk.Frame(root, bg=LINE, height=1).pack(fill="x", padx=10)
foot = tk.Label(root, text="", bg=BG, fg=MUT, font=FONT_S, anchor="w",
                justify="left", wraplength=360)
foot.pack(fill="x", padx=10, pady=(4, 8))


def card(parent, j):
    color = RUN if not j["done"] else (OK if j["status"].startswith("Completed") else BAD)
    f = tk.Frame(parent, bg=CARD, highlightbackground=color, highlightthickness=1)
    f.pack(fill="x", pady=4)
    top = tk.Frame(f, bg=CARD)
    top.pack(fill="x", padx=8, pady=(6, 0))
    tk.Label(top, text="●", bg=CARD, fg=color, font=FONT).pack(side="left")
    tk.Label(top, text=j["id"], bg=CARD, fg=MUT, font=FONT_S).pack(side="left", padx=(2, 0))
    tk.Label(top, text=j["worker"], bg=LINE, fg=FG, font=FONT_S).pack(side="right")
    tk.Label(f, text=j["desc"], bg=CARD, fg=FG, font=FONT_B, anchor="w",
             wraplength=330, justify="left").pack(fill="x", padx=8, pady=(2, 0))
    stat = "執行中" if not j["done"] else j["status"]
    tk.Label(f, text=f"{stat} · {fmt(j['elapsed'])}", bg=CARD, fg=MUT,
             font=FONT, anchor="w").pack(fill="x", padx=8)
    tail = (j.get("tail") or "").splitlines()
    if tail:
        tk.Label(f, text=tail[-1][:80], bg=CARD, fg=MUT, font=FONT_S, anchor="w",
                 wraplength=330, justify="left").pack(fill="x", padx=8, pady=(0, 6))
    else:
        tk.Frame(f, bg=CARD, height=6).pack()


def tick():
    data, err = fetch()
    if err is not None:
        hub_line1.config(text="⚠ 等待 hub 連線…")
        hub_line2.config(text=f"{API}\n（hub 或 session 可能尚未就緒）")
    else:
        h = data.get("hub", {})
        hub_line1.config(
            text=f"agent-hub · uptime {fmt(h.get('uptime', 0))}")
        hub_line2.config(
            text=f"workers: {', '.join(h.get('workers', [])) or '—'}\n"
                 f"jobs: {h.get('total', 0)}  |  執行中 {h.get('running', 0)}"
                 f"  ✓ {h.get('done', 0)}  ✗ {h.get('failed', 0)}")
        for w in body.winfo_children():
            w.destroy()
        jobs = data.get("jobs", [])
        if not jobs:
            tk.Label(body, text="（尚未派出任何 job）", bg=BG, fg=MUT,
                     font=FONT).pack(pady=12)
        else:
            for j in jobs:
                card(body, j)
        evs = h.get("events", [])
        foot.config(text="hub 最近：" + ("；".join(e["msg"] for e in evs[-3:]) or "—"))
    root.after(2000, tick)


tick()
root.mainloop()
