"""桌面懸浮儀表板（Tkinter，always-on-top，Windows 11 毛玻璃）。

由 mcp_worker_hub.py 的 open_dashboard() 以子進程彈出：
    python dashboard_float.py <port>
每 2 秒 fetch http://127.0.0.1:<port>/api，顯示：
  - agent-hub 自身執行狀態（啟用 workers、uptime、running/done/failed、最近工具）
  - 每個 job 的派工狀態（worker、描述、狀態、耗時、當下 log 尾行）

純 stdlib（tkinter + ctypes + urllib），無第三方相依。

外觀（比照 Apple UI，見 issue #8）：Win11 走 DWM Acrylic 毛玻璃 + 圓角 + 深色標題列
（ctypes，仍是 stdlib）；舊系統／非 Windows 靜默退回實心深色底。
刷新採「就地更新」——只改變動的那幾格、不整批砍掉重建，消除舊版每 tick 重建造成的閃爍。
"""
import json
import os
import sys
import tkinter as tk
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8787"
API = f"http://127.0.0.1:{PORT}/api"

# 深色玻璃調色盤。BACKDROP_GLASS 會被設成 -transparentcolor，讓 Acrylic 模糊從這些像素透出；
# 帶文字的元件一律鋪在不透明的 PANEL / CARD 上，避免 chroma-key 造成文字毛邊。
BACKDROP_GLASS = "#0b0e13"   # 玻璃生效時 = 透明鏤空（顯示背後模糊）
BACKDROP_SOLID = "#111319"   # 退回實心時的底色
PANEL = "#1b1e26"
CARD = "#20242e"
FG, MUT, FAINT, LINE = "#f2f3f5", "#9aa0ac", "#6b7280", "#2a2f3a"
RUN, OK, BAD = "#ff9f0a", "#30d158", "#ff453a"   # Apple 系統色：琥珀 / 綠 / 紅

FONT = ("Microsoft JhengHei UI", 9)
FONT_B = ("Microsoft JhengHei UI", 10, "bold")
FONT_S = ("Consolas", 8)


def fmt(secs):
    return f"{secs // 60}m{secs % 60:02d}s"


def fetch():
    try:
        with urllib.request.urlopen(API, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:
        return None, e


def _enable_windows_glass(win):
    """Win10/11：套 DWM Acrylic 毛玻璃 + 圓角 + 深色標題列。回傳 True 表示玻璃已生效。
    失敗（舊系統／非 Windows／API 不存在）就靜默回 False，呼叫端改用實心深色底。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        win.update_idletasks()

        user32 = ctypes.windll.user32
        # 64-bit HWND 一定要用 c_void_p，否則預設 c_int 會截斷 handle。
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = user32.GetParent(win.winfo_id())

        class _Accent(ctypes.Structure):
            _fields_ = [("state", ctypes.c_uint), ("flags", ctypes.c_uint),
                        ("grad", ctypes.c_uint), ("anim", ctypes.c_uint)]

        class _WCAData(ctypes.Structure):
            _fields_ = [("attr", ctypes.c_int),
                        ("data", ctypes.POINTER(_Accent)),
                        ("size", ctypes.c_size_t)]

        # ACCENT_ENABLE_ACRYLICBLURBEHIND=4；grad 為 AABBGGRR，AA 是玻璃染色濃度（越高越不透）。
        accent = _Accent(4, 0, 0x99110E0B, 0)
        payload = _WCAData(19, ctypes.pointer(accent), ctypes.sizeof(_Accent))  # WCA_ACCENT_POLICY=19
        swca = user32.SetWindowCompositionAttribute
        swca.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WCAData)]
        swca.restype = ctypes.c_int
        ok = swca(hwnd, ctypes.byref(payload))

        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        dwm.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        dark = ctypes.c_int(1)
        dwm(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))   # DWMWA_USE_IMMERSIVE_DARK_MODE
        corner = ctypes.c_int(2)                                 # DWMWCP_ROUND
        dwm(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))  # DWMWA_WINDOW_CORNER_PREFERENCE
        return bool(ok)
    except Exception:
        return False


root = tk.Tk()
root.title("🚀 派工儀表板")
root.attributes("-topmost", True)
root.geometry("380x560+40+80")
root.minsize(300, 300)

GLASS = _enable_windows_glass(root)
BACKDROP = BACKDROP_GLASS if GLASS else BACKDROP_SOLID
root.configure(bg=BACKDROP)
if GLASS:
    try:
        # 讓 BACKDROP 像素透明，Acrylic 模糊才透得出來。失敗就退回實心。
        root.attributes("-transparentcolor", BACKDROP)
    except tk.TclError:
        GLASS = False
        BACKDROP = BACKDROP_SOLID
        root.configure(bg=BACKDROP)

# --- 頂部：hub 自身狀態（鋪在不透明 PANEL 上，文字才清晰）---
head = tk.Frame(root, bg=PANEL)
head.pack(fill="x", padx=8, pady=(8, 0))
hub_line1 = tk.Label(head, text="連線中…", bg=PANEL, fg=FG, font=FONT_B, anchor="w")
hub_line1.pack(fill="x", padx=10, pady=(8, 0))
hub_line2 = tk.Label(head, text="", bg=PANEL, fg=MUT, font=FONT, anchor="w", justify="left")
hub_line2.pack(fill="x", padx=10, pady=(0, 8))

# --- 中段：可捲動的 job 卡片 ---
mid = tk.Frame(root, bg=BACKDROP)
mid.pack(fill="both", expand=True, padx=6, pady=6)
canvas = tk.Canvas(mid, bg=BACKDROP, highlightthickness=0)
sb = tk.Scrollbar(mid, orient="vertical", command=canvas.yview)
body = tk.Frame(canvas, bg=BACKDROP)
body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
canvas.create_window((0, 0), window=body, anchor="nw", width=352)
canvas.configure(yscrollcommand=sb.set)
canvas.pack(side="left", fill="both", expand=True)
sb.pack(side="right", fill="y")
root.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

empty = tk.Label(body, text="（尚未派出任何 job）", bg=PANEL, fg=MUT, font=FONT)

# --- 底部：hub 最近事件 ---
footbar = tk.Frame(root, bg=PANEL)
footbar.pack(fill="x", padx=8, pady=(0, 8))
foot = tk.Label(footbar, text="", bg=PANEL, fg=MUT, font=FONT_S, anchor="w",
                justify="left", wraplength=344)
foot.pack(fill="x", padx=10, pady=6)

# job_id -> 該卡片的可變動元件參照。就地更新用，避免每 tick 砍掉重建（舊版閃爍主因）。
cards = {}


def _state_color(j):
    if not j["done"]:
        return RUN
    return OK if j["status"].startswith("Completed") else BAD


def build_card(j):
    color = _state_color(j)
    f = tk.Frame(body, bg=CARD, highlightbackground=color, highlightthickness=1)
    f.pack(fill="x", pady=4, padx=2)
    top = tk.Frame(f, bg=CARD)
    top.pack(fill="x", padx=8, pady=(6, 0))
    dot = tk.Label(top, text="●", bg=CARD, fg=color, font=FONT)
    dot.pack(side="left")
    tk.Label(top, text=j["id"], bg=CARD, fg=FAINT, font=FONT_S).pack(side="left", padx=(3, 0))
    tk.Label(top, text=j["worker"], bg=PANEL, fg=MUT, font=FONT_S, padx=6).pack(side="right")
    tk.Label(f, text=j["desc"], bg=CARD, fg=FG, font=FONT_B, anchor="w",
             wraplength=320, justify="left").pack(fill="x", padx=8, pady=(2, 0))
    stat = tk.Label(f, text="", bg=CARD, fg=MUT, font=FONT, anchor="w")
    stat.pack(fill="x", padx=8)
    tail = tk.Label(f, text="", bg=CARD, fg=FAINT, font=FONT_S, anchor="w",
                    wraplength=320, justify="left")
    tail.pack(fill="x", padx=8, pady=(0, 6))
    return {"frame": f, "dot": dot, "stat": stat, "tail": tail, "color": color}


def update_card(c, j):
    color = _state_color(j)
    if color != c["color"]:
        c["frame"].config(highlightbackground=color)
        c["dot"].config(fg=color)
        c["color"] = color
    stat = "執行中" if not j["done"] else j["status"]
    c["stat"].config(text=f"{stat} · {fmt(j['elapsed'])}")
    tl = (j.get("tail") or "").splitlines()
    c["tail"].config(text=tl[-1][:80] if tl else "")


def tick():
    data, err = fetch()
    if err is not None:
        hub_line1.config(text="⚠ 等待 hub 連線…")
        hub_line2.config(text=f"{API}\n（hub 或 session 可能尚未就緒）")
    else:
        h = data.get("hub", {})
        hub_line1.config(text=f"agent-hub · uptime {fmt(h.get('uptime', 0))}")
        hub_line2.config(
            text=f"workers: {', '.join(h.get('workers', [])) or '—'}\n"
                 f"jobs {h.get('total', 0)} · 執行中 {h.get('running', 0)}"
                 f" · ✓ {h.get('done', 0)} · ✗ {h.get('failed', 0)}")

        jobs = data.get("jobs", [])
        seen = set()
        for j in jobs:
            seen.add(j["id"])
            if j["id"] in cards:
                update_card(cards[j["id"]], j)
            else:
                cards[j["id"]] = build_card(j)
        for jid in list(cards):          # 保險：移除已消失的 job（正常只增不減）
            if jid not in seen:
                cards[jid]["frame"].destroy()
                del cards[jid]

        if jobs and empty.winfo_ismapped():
            empty.pack_forget()
        elif not jobs and not empty.winfo_ismapped():
            empty.pack(pady=12)

        evs = h.get("events", [])
        foot.config(text="hub 最近：" + ("；".join(e["msg"] for e in evs[-3:]) or "—"))
    root.after(2000, tick)


tick()
root.mainloop()
