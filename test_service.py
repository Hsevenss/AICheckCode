#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AICheckCode 服务测试脚本
"""

import json
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "http://localhost:5000"


def api_get(path: str) -> dict:
    """GET 请求"""
    req = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))


def api_post(path: str, data: dict) -> dict:
    """POST 请求（兼容非 2xx 响应）"""
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(data).encode('utf-8'),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode('utf-8'))
        # 将 HTTP 状态码和错误信息一并返回，不抛异常
        return {"_status": e.code, **body}


def test_health():
    """测试健康检查"""
    print("=" * 60)
    print("1. 健康检查")
    print("=" * 60)
    result = api_get("/api/health")
    print(f"   状态: {result['status']}")
    print(f"   版本: {result['version']}")
    assert result['status'] == 'ok'
    print("   ✅ 通过\n")


def test_list_testcases():
    """测试列出测试用例文件"""
    print("=" * 60)
    print("2. 列出测试用例文件")
    print("=" * 60)
    result = api_get("/api/testcases")
    print(f"   可用文件: {result['test_cases']}")
    assert len(result['test_cases']) > 0
    print("   ✅ 通过\n")


def test_trigger_missing_params():
    """测试参数校验"""
    print("=" * 60)
    print("3. 参数校验测试")
    print("=" * 60)

    # 缺少 project_name
    result = api_post("/api/trigger", {
        "repository_url": "https://cnb.cool/walker_jun/OpenTest",
        "test_case_file": "test_sample.xlsx"
    })
    print(f"   缺少 project_name: {result['error']}")
    assert '缺少参数' in result['error']

    # 缺少 test_case_file
    result = api_post("/api/trigger", {
        "project_name": "test",
        "repository_url": "https://cnb.cool/walker_jun/OpenTest"
    })
    print(f"   缺少 test_case_file: {result['error']}")
    assert '缺少参数' in result['error']

    # 不存在的测试用例文件
    result = api_post("/api/trigger", {
        "project_name": "test",
        "repository_url": "https://cnb.cool/walker_jun/OpenTest",
        "test_case_file": "nonexistent.xlsx"
    })
    print(f"   文件不存在: {result['error']}")
    print(f"   可用文件提示: {result.get('available_files', [])}")
    assert '不存在' in result['error']
    print("   ✅ 全部通过\n")


def test_trigger_and_poll():
    """触发任务并轮询结果"""
    print("=" * 60)
    print("4. 触发任务并轮询结果")
    print("=" * 60)

    result = api_post("/api/trigger", {
        "project_name": "OpenTest",
        "repository_url": "https://cnb.cool/walker_jun/OpenTest",
        "test_case_file": "test_sample.xlsx"
    })

    task_id = result['task_id']
    print(f"   任务已创建: task_id={task_id}")
    print(f"   状态: {result['status']}")
    assert result['status'] == 'pending'

    # 轮询等待完成
    max_wait = 180  # 最多等 3 分钟
    poll_interval = 10
    waited = 0

    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval

        task = api_get(f"/api/task/{task_id}")
        status = task['status']
        progress = task.get('progress', '')
        print(f"   [{waited}s] status={status}, progress={progress}")

        if status in ('completed', 'failed'):
            break

    assert task['status'] == 'completed', f"任务未完成: {task.get('error', '')}"
    print("   ✅ 任务完成\n")

    # 打印结果
    print("=" * 60)
    print("5. 任务结果详情")
    print("=" * 60)

    # Diff 信息
    diff = task.get('diff', {})
    print(f"\n   --- Diff 信息 ---")
    print(f"   变更文件数: {diff.get('file_count', 0)}")
    print(f"   新增行数: {diff.get('additions', 0)}")
    print(f"   删除行数: {diff.get('deletions', 0)}")

    # 风险评估
    risk = task.get('risk_assessment', {})
    print(f"\n   --- 风险评估 ---")
    print(f"   风险等级: {risk.get('level', 'N/A')}")
    print(f"   风险分数: {risk.get('score', 'N/A')}")
    print(f"   高风险文件: {risk.get('high_risk_files', [])}")
    print(f"   评估理由: {risk.get('reason', 'N/A')[:200]}...")

    # 测试结果
    summary = task.get('summary', {})
    print(f"\n   --- 测试结果汇总 ---")
    print(f"   总计: {summary.get('total', 0)}")
    print(f"   通过: {summary.get('pass', 0)}")
    print(f"   失败: {summary.get('fail', 0)}")
    print(f"   异常: {summary.get('error', 0)}")
    print(f"   通过率: {summary.get('pass_rate', 'N/A')}")

    # 逐条结果
    print(f"\n   --- 逐条测试结果 ---")
    for t in task.get('test_results', []):
        icon = "✅" if t['success'] else "❌"
        print(f"   {icon} {t['case_id']}: {t['result']}")
        print(f"      描述: {t['description']}")
        print(f"      理由: {t['validation_reason']}")
        print()

    return task_id


def test_task_not_found():
    """测试查询不存在的任务"""
    print("=" * 60)
    print("6. 查询不存在的任务")
    print("=" * 60)
    try:
        api_get("/api/task/nonexistent-task-id")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode('utf-8'))
        print(f"   返回: {body['error']}")
        assert e.code == 404
        print("   ✅ 通过\n")


def test_list_tasks():
    """测试列出所有任务"""
    print("=" * 60)
    print("7. 列出所有任务")
    print("=" * 60)
    result = api_get("/api/tasks")
    tasks = result.get('tasks', [])
    print(f"   历史任务数: {len(tasks)}")
    for t in tasks:
        print(f"   - {t['task_id'][:8]}... | {t['project_name']} | {t['status']}")
    print("   ✅ 通过\n")


if __name__ == "__main__":
    try:
        test_health()
        test_list_testcases()
        test_trigger_missing_params()
        task_id = test_trigger_and_poll()
        test_task_not_found()
        test_list_tasks()

        print("=" * 60)
        print("🎉 全部测试通过!")
        print("=" * 60)

    except urllib.error.URLError as e:
        print(f"\n❌ 连接失败: {e}")
        print("   请确保服务已启动: cd AICheckCode && python3 app.py")
        sys.exit(1)
    except AssertionError as e:
        print(f"\n❌ 测试断言失败: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
        sys.exit(130)
