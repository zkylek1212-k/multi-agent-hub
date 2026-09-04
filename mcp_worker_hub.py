"""Local Agent Hub — 可抽換的 Multi-Agent 派工 MCP Server。

設定全走環境變數（stdio 下沒有 TTY，不能用互動選單）：
  HUB_WORKERS        逗號分隔的 worker 名單，留空 = 全部可用的都開
  HUB_BIN_<WORKER>   指定某 worker 的執行檔絕對路徑（繞開 PATH 與 .cmd shim）
  HUB_LOG_DIR        log 目錄，預設 .hub_logs
  HUB_WAIT_SLICE     wait_for_job 單次等待秒數，預設 45

完整說明見 ARCHITECTURE.md，部署見 INSTALL.md。
"""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# mcp 2.0 把 FastMCP 改名成 MCPServer。兩者的 .tool() 與 .run(transport=) 相同，
# 所以吃哪個版本都能跑，不必 pin mcp<2。
try:
    from mcp.server.fastmcp import FastMCP as _Server      # mcp 1.x
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp 2.x

# --- Worker 指令表 -----------------------------------------------------
# prompt 不放在命令列上（見下方 HANDOFF），這裡只放固定旗標。
# 慣例：接受 prompt 的旗標一律放在**最後**，避免它把後面的旗標當成自己的值。
WORKER_CMDS = {
    # ollama：.exe，參數安全
    "ollama":  ["ollama", "run", "qwen2.5-coder:70b"],

    # Claude Code：-p 不配權限旗標時，背景執行會停在工具核准而卡死。
    # --strict-mcp-config：worker 的 cwd 是本 repo 的 worktree，不擋的話它會
    # 載入專案 .mcp.json 拿到本 hub 的工具，變成可以再派工（遞迴分裂）。
    "claude_cli": ["claude", "--strict-mcp-config",
                   "--dangerously-skip-permissions", "-p"],

    # Codex：非互動子指令是 exec（沒有 --prompt / --yes 這種旗標）。
    # -a 必須在 exec 之「前」，--sandbox 必須在 exec 之「後」；
    # exec 預設 sandbox 是 read-only，不改成 workspace-write 就無法寫檔。
    "codex_cli":  ["codex", "-a", "never", "exec", "--sandbox", "workspace-write"],

    # Antigravity CLI：執行檔是 agy（不是 antigravity，也沒有 run 子指令）。
    # --print-timeout 預設只有 5m，長任務一定要加大。
    # 已知問題：非 TTY 下 -p 可能丟失 stdout，故指定 --output-format json。
    "agy_cli":    ["agy", "--dangerously-skip-permissions",
                   "--output-format", "json", "--print-timeout", "30m", "-p"],
}

# 交給 Worker 的命令列字串：純 ASCII、無引號、無 %，任何 shell 都不會改動它。
# 真正的任務內容寫在 worktree 的 .hub_prompt.md 裡。
PROMPT_FILE = ".hub_prompt.md"
HANDOFF = ("Read the file " + PROMPT_FILE + " in the current directory "
           "and carry out the task described in it.")

LOG_DIR = Path(os.environ.get("HUB_LOG_DIR", ".hub_logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
WAIT_SLICE = int(os.environ.get("HUB_WAIT_SLICE", "45"))


def _resolve(name: str) -> str | None:
    """解析執行檔路徑。Windows 上優先找 .exe：走 .cmd/.bat 會被 cmd.exe
    重新解析參數（引號竄改、%VAR% 展開、8191 字上限），見文件 §6。"""
    if os.name == "nt":
        p = shutil.which(name + ".exe")
        if p:
            return p
    return shutil.which(name)


# 啟動時解析一次。HUB_BIN_<WORKER> 可直接指定絕對路徑，繞開 PATH 與 .cmd shim。
_RESOLVED: dict[str, list[str]] = {}
for _k, _base in WORKER_CMDS.items():
    _exe = os.environ.get("HUB_BIN_" + _k.upper()) or _resolve(_base[0])
    if not _exe or not Path(_exe).is_file():
        continue
    _RESOLVED[_k] = [_exe, *_base[1:]]
    if _exe.lower().endswith((".cmd", ".bat")):
        print(f"[hub] 警告：{_k} 解析到批次檔 {_exe}。prompt 以檔案傳遞故無注入風險，"
              f"但仍建議設 HUB_BIN_{_k.upper()} 指向真正的 .exe。", file=sys.stderr)

_want = [w.strip() for w in os.environ.get("HUB_WORKERS", "").split(",") if w.strip()]
_missing = [w for w in _want if w not in _RESOLVED]
ACTIVE = [w for w in (_want or list(_RESOLVED)) if w in _RESOLVED]

# 重要：stdout 是 JSON-RPC 管線，任何 print 都會破壞協議。log 一律走 stderr。
print(f"[hub] active workers: {ACTIVE}", file=sys.stderr)
if _missing:
    print(f"[hub] 找不到執行檔，已停用：{_missing}", file=sys.stderr)
if not ACTIVE:
    print("[hub] 沒有任何可用的 Worker，請檢查 PATH 或 HUB_BIN_* 設定。", file=sys.stderr)

mcp = _Server("Local-Agent-Hub")
jobs: dict[str, dict] = {}          # job_id -> {state, log, done: asyncio.Event}
_tasks: set[asyncio.Task] = set()   # 保留 task 強參考，否則背景任務可能被 GC

# hub 自身的執行狀態：啟動時間 + 最近事件環狀緩衝（給懸浮儀表板顯示「hub 正在做什麼」）
_HUB: dict = {"started": time.monotonic(), "events": []}


def _split_args(args: str) -> list[str]:
    """拆解指令字串。Windows 上 POSIX 模式的 shlex 會把反斜線當跳脫字元吃掉，
    `C:\\proj\\wt1` 會變成 `C:projwt1`（git 會拿它當 drive-relative path 亂建目錄），
    所以 nt 改用非 POSIX 模式再自己去引號。"""
    if os.name != "nt":
        return shlex.split(args)
    toks = shlex.split(args, posix=False)
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in toks]


def _hub_event(text: str) -> None:
    _HUB["events"].append((time.time(), text))
    del _HUB["events"][:-30]   # 只留最近 30 筆


# --- 共用執行入口 ------------------------------------------------------
async def _exec(argv: list[str], cwd: str | None, timeout: int | None,
                log: Path | None = None, env: dict | None = None):
    """以 argv 執行外部程式。回傳 (returncode, 輸出)。

    給 log 時輸出直接寫檔，這樣逾時被 kill 也留得住終止前的輸出。
    """
    if not argv:
        return 1, "[Error] argv 為空"
    exe = argv[0] if Path(argv[0]).is_file() else _resolve(argv[0])
    if not exe:
        return 127, f"[Error] 找不到執行檔: {argv[0]}，請確認已安裝並加入 PATH"
    real = [exe, *argv[1:]]

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    if log is None:
        p = await asyncio.create_subprocess_exec(
            *real, cwd=cwd, env=merged_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            return 124, f"[Timeout] 超過 {timeout}s，已強制終止"
        return p.returncode, out.decode(errors="replace")

    with open(log, "wb") as f:
        p = await asyncio.create_subprocess_exec(
            *real, cwd=cwd, env=merged_env, stdout=f, stderr=asyncio.subprocess.STDOUT)
        try:
            await asyncio.wait_for(p.wait(), timeout)
            code = p.returncode
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            code = 124
    out = log.read_text(encoding="utf-8", errors="replace")
    if code == 124:
        out += f"\n[Timeout] 超過 {timeout}s，已強制終止（以上為終止前的輸出）"
        log.write_text(out, encoding="utf-8")
    return code, out


def _tail(text: str, n: int = 2000) -> str:
    """只把尾端回傳給 Master，避免整份 build log 灌爆 context。"""
    if len(text) <= n:
        return text
    return f"...（前 {len(text) - n} 字省略，完整內容見 log 檔）...\n{text[-n:]}"


# --- Tools ------------------------------------------------------------
@mcp.tool()
async def get_active_workers() -> str:
    """回報本次啟用的 Worker 與實際執行檔。不可派發給不在名單上的 Worker。"""
    lines = [f"  {w}: {_RESOLVED[w][0]}" for w in ACTIVE]
    return "Active Workers:\n" + "\n".join(lines)


@mcp.tool()
async def git(args: str, repo: str) -> str:
    """在指定 repo 執行 git 子指令。

    args 範例：
      "worktree add -b worker-task-1 D:/proj/wt-task-1"
      "worktree remove --force D:/proj/wt-task-1"
      "diff --stat"
      "diff -- src/foo.py"
      "merge --no-ff worker-task-1"
      "merge --abort"
    """
    sub = args.split()[0] if args.split() else args
    _hub_event(f"git {sub} …")
    code, out = await _exec(["git", *_split_args(args)], cwd=repo, timeout=120)
    return f"[git rc={code}]\n{_tail(out)}"


@mcp.tool()
async def delegate_to_worker(
    prompt: str,
    worker_type: str,
    working_dir: str,
    timeout_s: int = 1800,
    model: str | None = None,
) -> str:
    """非同步派工給 Worker。working_dir 必須是先前建立的 worktree 絕對路徑。

    prompt 會寫成 worktree 內的 .hub_prompt.md，命令列只傳一句固定英文指示。
    這樣 prompt 不受 shell 解析影響，也不受命令列長度限制。
    """
    if worker_type not in ACTIVE:
        return f"[Reject] Worker '{worker_type}' 未啟用。可用：{ACTIVE}"
    wt = Path(working_dir)
    if not wt.is_dir():
        return f"[Reject] working_dir 不存在：{working_dir}"

    job_id = uuid.uuid4().hex[:8]
    log_path = LOG_DIR / f"{job_id}.log"
    prompt_path = wt / PROMPT_FILE
    prompt_path.write_text(prompt, encoding="utf-8")
    (LOG_DIR / f"{job_id}.prompt.md").write_text(prompt, encoding="utf-8")

    # desc 給 list_jobs 當「這個 job 在做什麼」用：取 prompt 第一行非空白內容。
    desc = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "(無描述)")
    if len(desc) > 60:
        desc = desc[:57] + "..."

    jobs[job_id] = {
        "state": f"Running on {worker_type}",
        "log": str(log_path),
        "done": asyncio.Event(),
        "worker": worker_type,
        "desc": desc,
        "dir": working_dir,
        "started": time.monotonic(),
    }

    async def run_task():
        try:
            # 純 LLM（如 Ollama）沒有讀檔能力，直接餵 prompt；Agent 則傳 HANDOFF 讀檔指示
            custom_env = None
            if worker_type == "ollama":
                m = model or "qwen2.5-coder:70b"
                cmd_args = [_RESOLVED[worker_type][0], "run", m, prompt]
            elif worker_type == "agy_cli":
                cmd_args = [*_RESOLVED[worker_type], HANDOFF]
                if model:
                    cmd_args.insert(1, "--model")
                    cmd_args.insert(2, model)
            elif worker_type == "claude_cli":
                cmd_args = [*_RESOLVED[worker_type], HANDOFF]
                if model:
                    custom_env = {"CLAUDE_MODEL": model}
            else:
                cmd_args = [*_RESOLVED[worker_type], HANDOFF]

            code, out = await _exec(cmd_args,
                                    cwd=working_dir, timeout=timeout_s, log=log_path, env=custom_env)
            head = "Completed" if code == 0 else f"Failed (rc={code})"
            jobs[job_id]["state"] = f"{head}\n{_tail(out)}"
        except Exception as e:   # 背景 task 的例外預設會被吞掉，必須自己接住
            jobs[job_id]["state"] = f"Crashed: {e!r}"
        finally:
            prompt_path.unlink(missing_ok=True)   # 別留在 worktree 裡污染 diff
            jobs[job_id]["elapsed"] = time.monotonic() - jobs[job_id]["started"]
            jobs[job_id]["done"].set()
            _hub_event(f"job {job_id} 結束：{jobs[job_id]['state'].splitlines()[0]}")

    t = asyncio.create_task(run_task())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    _hub_event(f"派工 job {job_id} → {worker_type}" + (f" / {model}" if model else ""))
    return f"[Job Started] ID={job_id} worker={worker_type} dir={working_dir}"


@mcp.tool()
async def wait_for_job(job_ids: str, timeout_s: int = WAIT_SLICE) -> str:
    """等待任務結束。job_ids 可用逗號分隔，一次等一整批（建議這樣用）。

    單次等待長度由 HUB_WAIT_SLICE 決定。若回傳 [Still Running]，直接原樣再呼叫一次。
    """
    ids = [i.strip() for i in job_ids.split(",") if i.strip()]
    unknown = [i for i in ids if i not in jobs]
    if unknown:
        return f"[Error] 查無此 job: {unknown}"

    async def _all():
        await asyncio.gather(*(jobs[i]["done"].wait() for i in ids))

    try:
        await asyncio.wait_for(_all(), timeout_s)
    except asyncio.TimeoutError:
        pending = [i for i in ids if not jobs[i]["done"].is_set()]
        done = [i for i in ids if jobs[i]["done"].is_set()]
        msg = f"[Still Running] 仍在執行: {', '.join(pending)}"
        if done:
            msg += f"\n已完成: {', '.join(done)}"
        return msg + "\n請再次呼叫 wait_for_job（傳入同一批 id 即可）。"

    return "\n\n".join(
        f"[{i}] {jobs[i]['state']}\n(完整 log: {jobs[i]['log']})" for i in ids)


@mcp.tool()
async def list_jobs() -> str:
    """列出本次啟動以來所有 job 的狀態表，給使用者看的進度總覽。

    這是 hub 的真實記錄，不是 Master 的記憶——每次要向使用者回報進度時都該用它。
    """
    if not jobs:
        return "（尚未派出任何 job）"

    rows = ["| job_id | Worker | 狀態 | 耗時 | 任務 |",
            "| --- | --- | --- | --- | --- |"]
    for jid, j in jobs.items():
        first = j["state"].splitlines()[0] if j["state"] else "?"
        if j["done"].is_set():
            state = first                       # Completed / Failed (rc=N) / Crashed
        else:
            state = "執行中"
        secs = j.get("elapsed", time.monotonic() - j["started"])
        rows.append(f"| {jid} | {j['worker']} | {state} | {int(secs // 60)}m{int(secs % 60):02d}s "
                    f"| {j['desc']} |")
    return "\n".join(rows)


@mcp.tool()
async def check_job_status(job_id: str) -> str:
    """非阻塞查詢狀態。"""
    j = jobs.get(job_id)
    if not j:
        return "[Error] Job ID not found."
    return f"[{job_id}] {j['state']}\n(完整 log: {j['log']})"


@mcp.tool()
async def run_in_sandbox(
    command: str,
    worktree_path: str,
    image: str = "node:22-alpine",
    network: bool = False,
    timeout_s: int = 900,
) -> str:
    """在容器中對 worktree 執行指令。command 例：'npm test'、'pytest -q'。

    network 預設關閉。worktree 是乾淨 checkout，沒有 node_modules / venv，
    所以流程是：先 network=True 跑安裝（'npm ci'），再 network=False 跑測試。

    注意：worktree 的 .git 是指向主 repo 的絕對路徑檔，容器內解析不到，
    因此容器內不要跑任何 git 指令。
    """
    abs_path = Path(worktree_path).resolve().as_posix()   # Windows 的 D:\x 轉成 D:/x
    argv = [
        "docker", "run", "--rm",
        "--network", "bridge" if network else "none",
        "--memory", "2g", "--cpus", "2",
        "-v", f"{abs_path}:/app", "-w", "/app",
        image, "sh", "-c", command,
    ]
    _hub_event(f"sandbox: {command[:40]}")
    code, out = await _exec(argv, cwd=None, timeout=timeout_s)
    head = "Passed" if code == 0 else f"Failed (rc={code})"
    return f"[Sandbox {head}] (network={'on' if network else 'off'})\n{_tail(out)}"


# --- 即時派工儀表板 ----------------------------------------------------
# server 跑在 hub 進程內的 daemon thread，直接讀記憶體的 jobs dict + tail log 檔，
# 不需中介檔。頁面每 2 秒 fetch /api 自動刷新。open_dashboard() 由 Master 派工後呼叫。
_dash = {"port": None}


def _log_tail(path: str, n: int = 6) -> str:
    """回傳 log 尾端幾行非空內容，當作「worker 現在在做什麼」。"""
    try:
        lines = [ln for ln in Path(path).read_text(
            encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _dash_state() -> dict:
    out = []
    running = 0
    done_n = 0
    failed_n = 0
    for jid, j in jobs.items():
        done = j["done"].is_set()
        head = (j["state"].splitlines()[0] if j.get("state") else "?")
        secs = int(j.get("elapsed", time.monotonic() - j["started"]))
        if not done:
            running += 1
        elif head.startswith("Completed"):
            done_n += 1
        else:
            failed_n += 1
        out.append({
            "id": jid, "worker": j["worker"], "desc": j["desc"],
            "done": done,
            "status": head if done else "running",
            "elapsed": secs, "tail": _log_tail(j["log"]), "dir": j.get("dir", ""),
        })
    hub = {
        "workers": ACTIVE,
        "uptime": int(time.monotonic() - _HUB["started"]),
        "total": len(jobs), "running": running, "done": done_n, "failed": failed_n,
        "events": [{"t": int(t), "msg": m} for t, m in _HUB["events"][-8:]],
    }
    return {"jobs": out, "hub": hub, "ts": int(time.time())}


_DASH_HTML = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>派工儀表板</title><style>
:root{color-scheme:light dark;--bg:#0f1115;--card:#1a1e27;--fg:#e6e6e6;--mut:#8b93a7;
--run:#f0b429;--ok:#3ecf8e;--bad:#ff6b6b;--line:#2a2f3a}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,"Microsoft JhengHei",sans-serif;
background:var(--bg);color:var(--fg)}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;
align-items:center;gap:12px}
header h1{font-size:16px;margin:0}#meta{color:var(--mut);font-size:12px}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:16px}
.col h2{font-size:13px;color:var(--mut);margin:0 0 8px;text-transform:uppercase;
letter-spacing:.05em}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--mut);
border-radius:8px;padding:10px 12px;margin-bottom:10px}
.card.run{border-left-color:var(--run)}.card.ok{border-left-color:var(--ok)}
.card.bad{border-left-color:var(--bad)}
.card .top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.id{font-family:ui-monospace,monospace;color:var(--mut);font-size:12px}
.badge{font-size:11px;padding:1px 6px;border-radius:10px;background:#2a2f3a;color:var(--fg)}
.desc{margin:6px 0;font-weight:600}
.elapsed{color:var(--mut);font-size:12px}
pre{margin:8px 0 0;padding:8px;background:#0b0d12;border-radius:6px;font-size:11px;
color:var(--mut);white-space:pre-wrap;word-break:break-all;max-height:120px;overflow:auto}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.run{background:var(--run);animation:blink 1s infinite}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
@keyframes blink{50%{opacity:.3}}
.empty{color:var(--mut);font-size:12px;padding:8px 0}
</style></head><body>
<header><h1>🚀 派工即時儀表板</h1><span id="meta">連線中…</span></header>
<div class="cols">
<div class="col"><h2>執行中</h2><div id="run"></div></div>
<div class="col"><h2>已完成</h2><div id="ok"></div></div>
<div class="col"><h2>失敗 / 異常</h2><div id="bad"></div></div>
</div>
<script>
function fmt(s){var m=Math.floor(s/60),x=s%60;return m+"m"+(x<10?"0":"")+x+"s"}
function esc(t){var d=document.createElement("div");d.textContent=t||"";return d.innerHTML}
function card(j,cls){return '<div class="card '+cls+'"><div class="top">'+
'<span class="id">'+j.id+'</span><span class="badge">'+esc(j.worker)+'</span></div>'+
'<div class="desc"><span class="dot '+cls+'"></span>'+esc(j.desc)+'</div>'+
'<div class="elapsed">'+esc(cls==="run"?"執行中 · ":j.status+" · ")+fmt(j.elapsed)+'</div>'+
(j.tail?'<pre>'+esc(j.tail)+'</pre>':'')+'</div>'}
async function tick(){
 try{var r=await fetch("/api",{cache:"no-store"});var d=await r.json();
  var run=[],ok=[],bad=[];
  d.jobs.forEach(function(j){
   if(!j.done)run.push(card(j,"run"));
   else if(/^Completed/.test(j.status))ok.push(card(j,"ok"));
   else bad.push(card(j,"bad"));});
  document.getElementById("run").innerHTML=run.join("")||'<div class="empty">—</div>';
  document.getElementById("ok").innerHTML=ok.join("")||'<div class="empty">—</div>';
  document.getElementById("bad").innerHTML=bad.join("")||'<div class="empty">—</div>';
  document.getElementById("meta").textContent=
   d.jobs.length+" 個 job · 更新於 "+new Date(d.ts*1000).toLocaleTimeString();
 }catch(e){document.getElementById("meta").textContent="hub 連線中斷（可能 session 已關）";}
}
tick();setInterval(tick,2000);
</script></body></html>"""


class _DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # 靜音：預設會往 stderr 印每個請求
        pass

    def do_GET(self):
        if self.path.startswith("/api"):
            body = json.dumps(_dash_state()).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body = _DASH_HTML.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _launch_float(port: int) -> str:
    """彈出桌面懸浮視窗（Tkinter，always-on-top）。回傳狀態字串。"""
    script = Path(__file__).with_name("dashboard_float.py")
    if not script.is_file():
        return "（找不到 dashboard_float.py，改用瀏覽器開 URL）"
    argv = [sys.executable, str(script), str(port)]
    try:
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0))
            _dash["proc"] = subprocess.Popen(argv, creationflags=flags)
        else:
            _dash["proc"] = subprocess.Popen(argv, start_new_session=True)
        return "已彈出懸浮視窗"
    except Exception as e:   # GUI 起不來就退回瀏覽器
        return f"（懸浮視窗啟動失敗：{e!r}，改用瀏覽器開 URL）"


@mcp.tool()
async def open_dashboard() -> str:
    """啟動即時派工儀表板：起本機 HTTP server，並彈出一個桌面懸浮視窗（always-on-top，
    每 2 秒自動刷新）。視窗同時顯示「各 job 派工狀態」與「agent-hub 自身執行狀態」
    （啟用的 workers、uptime、running/done/failed 計數、最近在跑的工具）。

    Master 派工後呼叫一次即可（idempotent）。若懸浮視窗起不來（無桌面環境等），
    仍會回傳 URL，可改用瀏覽器開啟。
    """
    if not _dash["port"]:
        srv = None
        for p in range(8787, 8807):
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", p), _DashHandler)
                _dash["port"] = p
                break
            except OSError:
                continue
        if srv is None:
            return "[Error] 8787-8806 連接埠皆被占用，無法啟動儀表板。"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{_dash['port']}/"
    proc = _dash.get("proc")
    alive = proc is not None and proc.poll() is None
    note = "（懸浮視窗已在執行）" if alive else _launch_float(_dash["port"])
    return f"{note}\n備援 URL（懸浮視窗看不到時用瀏覽器開）：{url}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
