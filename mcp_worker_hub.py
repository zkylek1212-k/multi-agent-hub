"""Local Agent Hub — 可抽換的 Multi-Agent 派工 MCP Server。

設定全走環境變數（stdio 下沒有 TTY，不能用互動選單）：
  HUB_WORKERS        逗號分隔的 worker 名單，留空 = 全部可用的都開
  HUB_BIN_<WORKER>   指定某 worker 的執行檔絕對路徑（繞開 PATH 與 .cmd shim）
  HUB_LOG_DIR        log 目錄，預設 .hub_logs
  HUB_WAIT_SLICE     wait_for_job 單次等待秒數，預設 45

完整說明見 ARCHITECTURE.md，部署見 INSTALL.md。
"""

import asyncio
import os
import shlex
import shutil
import sys
import time
import uuid
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
    "local_70b":  ["ollama", "run", "qwen2.5-coder:70b"],

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


# --- 共用執行入口 ------------------------------------------------------
async def _exec(argv: list[str], cwd: str | None, timeout: int | None,
                log: Path | None = None):
    """以 argv 執行外部程式。回傳 (returncode, 輸出)。

    給 log 時輸出直接寫檔，這樣逾時被 kill 也留得住終止前的輸出。
    """
    if not argv:
        return 1, "[Error] argv 為空"
    exe = argv[0] if Path(argv[0]).is_file() else _resolve(argv[0])
    if not exe:
        return 127, f"[Error] 找不到執行檔: {argv[0]}，請確認已安裝並加入 PATH"
    real = [exe, *argv[1:]]

    if log is None:
        p = await asyncio.create_subprocess_exec(
            *real, cwd=cwd,
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
            *real, cwd=cwd, stdout=f, stderr=asyncio.subprocess.STDOUT)
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
    code, out = await _exec(["git", *shlex.split(args)], cwd=repo, timeout=120)
    return f"[git rc={code}]\n{_tail(out)}"


@mcp.tool()
async def delegate_to_worker(
    prompt: str,
    worker_type: str,
    working_dir: str,
    timeout_s: int = 1800,
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
            if worker_type == "local_70b":
                cmd_args = [*_RESOLVED[worker_type], prompt]
            else:
                cmd_args = [*_RESOLVED[worker_type], HANDOFF]

            code, out = await _exec(cmd_args,
                                    cwd=working_dir, timeout=timeout_s, log=log_path)
            head = "Completed" if code == 0 else f"Failed (rc={code})"
            jobs[job_id]["state"] = f"{head}\n{_tail(out)}"
        except Exception as e:   # 背景 task 的例外預設會被吞掉，必須自己接住
            jobs[job_id]["state"] = f"Crashed: {e!r}"
        finally:
            prompt_path.unlink(missing_ok=True)   # 別留在 worktree 裡污染 diff
            jobs[job_id]["elapsed"] = time.monotonic() - jobs[job_id]["started"]
            jobs[job_id]["done"].set()

    t = asyncio.create_task(run_task())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
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
    code, out = await _exec(argv, cwd=None, timeout=timeout_s)
    head = "Passed" if code == 0 else f"Failed (rc={code})"
    return f"[Sandbox {head}] (network={'on' if network else 'off'})\n{_tail(out)}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
