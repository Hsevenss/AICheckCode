#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终端轮询任务进度。

用法：
    python watch_task.py <task_id>              # 每 5 秒刷一次
    python watch_task.py <task_id> -i 2         # 每 2 秒
    python watch_task.py <task_id> --detail     # 结束后打印每条用例的校验理由
    python watch_task.py --latest               # 自动挑最近的一个任务

依赖：仅标准库。服务需已启动（默认 http://127.0.0.1:5000）。
"""
import sys
import time
import json
import argparse
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:5000"


def get(path):
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}
    except urllib.error.URLError as e:
        return {"_net_error": str(e.reason)}
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, e)}


def latest_task_id():
    d = get("/api/tasks")
    tasks = d.get("tasks") or []
    if not tasks:
        return None
    # 优先 running，其次 pending，最后按原顺序（接口已按状态排好）
    for t in tasks:
        if t.get("status") == "running":
            return t["task_id"]
    for t in tasks:
        if t.get("status") == "pending":
            return t["task_id"]
    return tasks[0]["task_id"]


def fmt_stamp():
    return time.strftime("%H:%M:%S")


def print_report(d, detail=False):
    print()
    print("=" * 70)
    print("项目    :", d.get("project_name"))
    print("仓库    :", d.get("repository_url"))
    print("用例文件:", d.get("test_case_file"))
    print("状态    :", d.get("status"))

    diff = d.get("diff")
    if diff:
        print("-" * 70)
        print("代码变更: %s 个文件，+%s / -%s" % (
            diff.get("file_count", 0), diff.get("additions", 0), diff.get("deletions", 0)))
        print("  commit: %s -> %s" % (
            str(diff.get("commit_hash_before"))[:8],
            str(diff.get("commit_hash_after"))[:8]))
        for f in (diff.get("changed_files") or [])[:10]:
            print("   -", f)

    risk = d.get("risk_assessment")
    if risk:
        print("-" * 70)
        print("风险评估: %s（%s 分）" % (risk.get("level"), risk.get("score")))
        print("  理由:", str(risk.get("reason"))[:200])

    summary = d.get("summary")
    if summary:
        print("-" * 70)
        print("汇总    : 总计 %s | 通过 %s | 失败 %s | 异常 %s | 通过率 %s" % (
            summary.get("total"), summary.get("pass"), summary.get("fail"),
            summary.get("error"), summary.get("pass_rate")))

    results = d.get("test_results") or []
    if results:
        print("-" * 70)
        print("逐条结果（%d 条）:" % len(results))
        for r in results:
            mark = {"通过": "√", "失败": "×"}.get(r.get("result"), "?")
            print("  %s %-8s %-28s %s" % (
                mark, r.get("case_id"), str(r.get("description"))[:28], r.get("result")))
            if detail:
                reason = str(r.get("validation_reason", "")).replace("\n", " ")
                print("        └ %s" % reason[:220])

    if d.get("error"):
        print("-" * 70)
        print("错误    :", d["error"])
    print("=" * 70)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", help="任务 ID")
    ap.add_argument("--latest", action="store_true", help="自动选择最近的任务")
    ap.add_argument("-i", "--interval", type=float, default=5.0, help="轮询间隔秒数")
    ap.add_argument("--detail", action="store_true", help="结束后打印每条校验理由")
    ap.add_argument("--base", default=BASE, help="服务地址")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")

    task_id = args.task_id
    if not task_id and args.latest:
        task_id = latest_task_id()
    if not task_id:
        print("请提供 task_id，或用 --latest")
        sys.exit(1)

    print("轮询任务 %s  每 %.0f 秒刷新  (Ctrl+C 退出，退出不影响服务继续跑)" % (task_id, args.interval))
    print("-" * 70)

    last_progress = None
    while True:
        d = get("/api/task/" + task_id)

        if "_net_error" in d:
            print("[%s] 连不上服务：%s" % (fmt_stamp(), d["_net_error"]))
            print("      确认服务已启动： python app.py")
            sys.exit(1)
        if "_http_error" in d:
            print("[%s] HTTP %s（任务不存在？）" % (fmt_stamp(), d["_http_error"]))
            sys.exit(1)
        if "_error" in d:
            print("[%s] %s" % (fmt_stamp(), d["_error"]))
            sys.exit(1)

        status = d.get("status")
        progress = d.get("progress", "")
        done = len(d.get("test_results") or [])

        if progress != last_progress or done:
            extra = ("  已完成 %d 条" % done) if done else ""
            print("[%s] %-9s %s%s" % (fmt_stamp(), status, progress, extra))
            last_progress = progress

        if status in ("completed", "failed"):
            print_report(d, detail=args.detail)
            return

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出轮询（服务端的任务会继续跑）")
