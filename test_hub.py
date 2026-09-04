"""hub 的最小自我檢查。install.ps1 第 8 步會跑這支。

沒有測試框架，`py -3 test_hub.py` 直接執行。
"""

import asyncio
import os
import sys

import mcp_worker_hub as h


def _fake_job(jid, worker, desc, state, done, elapsed=None):
    ev = asyncio.Event()
    if done:
        ev.set()
    h.jobs[jid] = {
        "state": state, "log": f"{jid}.log", "done": ev,
        "worker": worker, "desc": desc, "dir": "/tmp/wt",
        "started": h.time.monotonic() - (elapsed or 0),
    }
    if elapsed is not None and done:
        h.jobs[jid]["elapsed"] = elapsed


def main():
    # 1. server 起得來、工具數量正確
    assert h.ACTIVE is not None, "ACTIVE 未初始化"
    print(f"[1] active workers: {h.ACTIVE or '(無，檢查 PATH 或 HUB_BIN_*)'}")

    # 2. list_jobs 空表
    empty = asyncio.run(h.list_jobs())
    assert "尚未派出" in empty, empty
    print("[2] list_jobs 空狀態 OK")

    # 3. list_jobs 混合狀態：執行中的看即時耗時，結束的看凍結的 elapsed
    _fake_job("aaa11111", "claude_cli", "重構 auth 邏輯", "Running on claude_cli", done=False, elapsed=75)
    _fake_job("bbb22222", "agy_cli", "產生 model 樣板", "Completed\n改了 3 個檔", done=True, elapsed=125)
    _fake_job("ccc33333", "claude_cli", "整合層", "Failed (rc=1)\ntraceback...", done=True, elapsed=8)
    table = asyncio.run(h.list_jobs())
    print(table)
    for want in ("aaa11111", "claude_cli", "執行中", "1m15s",
                 "Completed", "2m05s", "Failed (rc=1)", "0m08s", "重構 auth 邏輯"):
        assert want in table, f"表格缺少 {want!r}\n{table}"
    # 多行 state 只能取第一行，否則表格會被撐爆
    assert "traceback" not in table and "改了 3 個檔" not in table, "state 沒有只取第一行"
    print("[3] list_jobs 狀態表 OK")

    # 4. 拒絕未啟用的 worker
    r = asyncio.run(h.delegate_to_worker("x", "not_a_worker", "."))
    assert r.startswith("[Reject]"), r
    print("[4] 未啟用 worker 已拒絕 OK")

    # 5. 未知 job id
    r = asyncio.run(h.wait_for_job("deadbeef"))
    assert "查無此 job" in r, r
    print("[5] 未知 job id 已回報 OK")

    # 6. Windows 反斜線路徑不能被 shlex 吃掉（吃掉的話 git 會在錯的地方建 worktree）。
    #    這是 nt 專屬行為：_split_args 只在 Windows 保留反斜線；POSIX 的 shlex 本來就把
    #    反斜線當跳脫，且 POSIX 不用反斜線路徑，故該斷言只在 nt 驗證（否則 Linux CI 必失敗）。
    if os.name == "nt":
        toks = h._split_args(r'worktree add C:\proj\wt1 worker-task-1')
        assert toks[2:] == [r"C:\proj\wt1", "worker-task-1"], toks
    assert h._split_args('diff -- "src/a b.py"')[-1] == "src/a b.py", "引號沒去掉"
    print("[6] 反斜線路徑拆解 OK")

    # 6b. _summarize_output：JSON 只抽 status+response，非 JSON 原樣
    big = ('{"conversation_id":"x","status":"SUCCESS","response":"改了 a.py",'
           '"usage":{"cache_read_tokens":1412215},"duration_seconds":241}')
    s = h._summarize_output(big)
    assert "改了 a.py" in s and "SUCCESS" in s, s
    assert "cache_read_tokens" not in s and "conversation_id" not in s, "usage 沒被濾掉"
    assert h._summarize_output("not json at all") == "not json at all"
    assert h._summarize_output('{"foo":1}') == '{"foo":1}', "沒 response 應原樣"
    print("[6b] _summarize_output 抽 status+response OK")

    # 7. 儀表板 server 不得搶佔已被佔用的 port（Windows 的 SO_REUSEADDR 會允許，
    #    搶到的話多個 session 的 hub 會全綁 8787，懸浮視窗顯示到別人的 job）
    first = h.ThreadingHTTPServer(("127.0.0.1", 0), h._DashHandler)
    try:
        port = first.server_address[1]
        try:
            h._DashServer(("127.0.0.1", port), h._DashHandler).server_close()
            raise AssertionError(f"_DashServer 搶佔了已被佔用的 port {port}")
        except OSError:
            pass
    finally:
        first.server_close()
    print("[7] 儀表板 port 不會被搶佔 OK")

    print("\nSMOKE ok active=" + ",".join(h.ACTIVE))


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nSELFTEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
