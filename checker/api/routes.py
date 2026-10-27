#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Flask API 路由
"""

import os
import json
import uuid
import logging
import threading
from typing import Dict, Optional

from flask import Blueprint, request, jsonify

from checker.core.config import config
from checker.core.models import TaskReport
from checker.excel_handler import ExcelHandler
from checker.git_handler import GitHandler
from checker.risk_analyzer import RiskAnalyzer
from checker.executor import TestExecutor
from checker.notifier import extract_risk_snippets, is_risky, notify_task_result

# 全局任务存储
_tasks: Dict[str, TaskReport] = {}
_tasks_lock = threading.Lock()

# 同项目并发控制（排队 · 最新胜出）：
# 项目正在跑 → 新请求覆盖式排队（旧排队被替换）；
# 任务结束时在 finally 里接力启动最新一条。
# 多次 push 合并成"最终状态"的一次检查 —— pull 后 HEAD@{1}→HEAD
# 的 diff 机制天然覆盖排队期间累积的所有提交。
_running_projects: Dict[str, str] = {}    # project_name -> 运行中 task_id
_pending_projects: Dict[str, dict] = {}  # project_name -> 最新一次排队参数
_project_lock = threading.Lock()

api_bp = Blueprint('api', __name__)
logger = logging.getLogger("API")


def _push_wechat(snapshot: dict):
    """后台线程：风险用例汇总推送 Server酱微信通知（内部 try/except 兜底）"""
    notify_task_result(
        task_id=snapshot["task_id"],
        project_name=snapshot["project_name"],
        repository_url=snapshot["repository_url"],
        test_case_file=snapshot["test_case_file"],
        test_results=snapshot["test_results"],
        summary=snapshot["summary"],
        risk_assessment=snapshot["risk_assessment"],
        commit=snapshot["commit"],
        logger=logger,
    )


def _get_tasks_dir() -> str:
    """获取任务持久化目录"""
    tasks_dir = os.path.join(config.workspaces_dir, ".tasks")
    os.makedirs(tasks_dir, exist_ok=True)
    return tasks_dir


def _save_task(task_id: str, report: TaskReport):
    """持久化任务到文件"""
    tasks_dir = _get_tasks_dir()
    filepath = os.path.join(tasks_dir, f"{task_id}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)


def _load_task(task_id: str) -> Optional[TaskReport]:
    """从文件加载任务"""
    tasks_dir = _get_tasks_dir()
    filepath = os.path.join(tasks_dir, f"{task_id}.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            report = TaskReport(**data)
            return report
        except Exception:
            return None
    return None


def _get_task(task_id: str) -> Optional[TaskReport]:
    """获取任务（先从内存，再从文件）"""
    with _tasks_lock:
        if task_id in _tasks:
            return _tasks[task_id]
    return _load_task(task_id)


def _start_or_queue(project_name: str, repository_url: str,
                   test_case_file: str, source: str = "manual"):
    """
    统一的任务启动入口（/api/trigger、/api/webhook、将来的定时器都走这里）。

    项目空闲 → 立即创建任务并启动后台线程；
    项目在跑 → 覆盖式排队（最新胜出），当前任务结束时接力启动。

    Returns:
        (task_id, "started") 或 (运行中的 task_id, "queued")
    """
    with _project_lock:
        if project_name in _running_projects:
            _pending_projects[project_name] = {
                "project_name": project_name,
                "repository_url": repository_url,
                "test_case_file": test_case_file,
                "source": source,
            }
            running_id = _running_projects[project_name]
            logger.info(f"项目 {project_name} 有任务在跑（{running_id[:8]}），"
                        f"新请求（{source}）已排队，最新胜出")
            return running_id, "queued"

        task_id = str(uuid.uuid4())
        _running_projects[project_name] = task_id

    try:
        report = TaskReport(
            project_name=project_name,
            repository_url=repository_url,
            test_case_file=test_case_file,
            status="pending",
            progress="任务已创建，等待执行"
        )
        with _tasks_lock:
            _tasks[task_id] = report
        _save_task(task_id, report)

        thread = threading.Thread(
            target=_run_task,
            args=(task_id, project_name, repository_url, test_case_file, source),
            daemon=True
        )
        thread.start()
    except Exception:
        # 启动失败要释放项目占用，否则该项目永久卡死
        with _project_lock:
            _running_projects.pop(project_name, None)
        raise

    logger.info(f"任务已创建：{task_id}, project={project_name}, source={source}")
    return task_id, "started"


def _run_task(task_id: str, project_name: str, repository_url: str,
              test_case_file: str, source: str = "manual"):
    """
    后台执行任务

    流程：
    1. Git clone/pull
    2. 获取 diff
    3. 风险评估
    4. 加载测试用例
    5. 并发执行测试
    6. 汇总报告
    """
    task_logger = logging.getLogger(f"Task-{task_id[:8]}")
    task_logger.info(f"任务开始（来源：{source}）：{project_name}")

    try:
        # 更新状态为 running
        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].status = "running"
                _tasks[task_id].progress = "正在拉取代码..."

        # 1. Git 操作
        task_logger.info(f"开始 Git 操作：{repository_url}")
        git_handler = GitHandler()
        project_path = git_handler.clone_or_pull(repository_url, project_name)
        task_logger.info(f"Git 操作完成，路径：{project_path}")

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].progress = "代码拉取完成，正在获取变更内容..."

        # 2. 获取 diff
        task_logger.info("获取代码变更内容")
        diff_info = git_handler.get_diff(project_path)
        task_logger.info(f"变更文件数：{len(diff_info.changed_files)}")

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].diff = diff_info.to_dict()
                _tasks[task_id].progress = "变更获取完成，正在进行风险评估..."

        # 3. 风险评估
        task_logger.info("开始风险评估")
        risk_analyzer = RiskAnalyzer()
        risk_assessment = risk_analyzer.assess(diff_info, project_path)
        task_logger.info(f"风险评估完成：等级={risk_assessment.level}")

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].risk_assessment = risk_assessment.to_dict()
                _tasks[task_id].progress = "风险评估完成，正在加载测试用例..."

        # 3.5 风险分级 → 执行策略决策
        # 低风险走 inline 快速验证（DeepSeek 直连，前缀缓存便宜），
        # 中/高风险走 Agent 模式（模型用 Read/Glob/Grep 自主读码），
        # 高风险再降并发 + 放宽 max_turns，宁慢勿错。
        # 未知等级按"中"处理（保守兜底）。
        level = risk_assessment.level
        rs = config.get("risk_strategy", {}) or {}
        if level not in ("高", "中", "低"):
            level = rs.get("unknown_as", "中")
        default_strat = {"analysis_mode": "agent", "llm_mode": "sdk",
                         "scope": "diff_related", "concurrency": 8, "max_turns": 5}
        strat = dict(rs.get(level) or default_strat)

        # 聚焦文件集合：diff 变更文件 ∪ 风险评估给出的重点文件
        focus = sorted({str(f).replace("\\", "/")
                        for f in list(diff_info.changed_files)
                        + list(risk_assessment.high_risk_files) if f})

        # 预算兜底：低风险 inline 时，若聚焦文件总量超采集预算（大项目大改动），
        # 强制升级 Agent —— 按需读码天生适合装不下的大仓库
        if strat.get("analysis_mode") == "inline" and focus:
            budget = int(config.get("max_total_bytes", 150)) * 1024
            focus_total = 0
            for rel in focus:
                fp = os.path.join(project_path, rel)
                if os.path.isfile(fp):
                    focus_total += os.path.getsize(fp)
            if focus_total > budget:
                strat.update({"analysis_mode": "agent", "llm_mode": "sdk",
                              "scope": "diff_related", "escalated": True})
                task_logger.warning(
                    f"聚焦文件总量 {focus_total} 字节超预算 {budget}，已升级 Agent 模式")

        strat["focus_files"] = focus
        task_logger.info(
            f"风险等级：{level}，执行策略：mode={strat.get('analysis_mode')}, "
            f"llm={strat.get('llm_mode')}, scope={strat.get('scope')}, "
            f"并发={strat.get('concurrency')}, max_turns={strat.get('max_turns')}")

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].progress = (
                    f"风险等级：{level}，采用 "
                    f"{'Agent' if strat.get('analysis_mode') == 'agent' else 'inline'}"
                    f"{'深度' if level == '高' else ''}验证，正在加载测试用例...")

        # 4. 加载测试用例
        test_cases_path = os.path.join(config.test_cases_dir, test_case_file)
        task_logger.info(f"加载测试用例：{test_cases_path}")

        if not os.path.exists(test_cases_path):
            raise FileNotFoundError(f"测试用例文件不存在：{test_cases_path}")

        excel_handler = ExcelHandler(test_cases_path)
        test_cases = excel_handler.get_test_cases()
        excel_handler.close()
        task_logger.info(f"已加载 {len(test_cases)} 个测试用例")

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].progress = f"已加载 {len(test_cases)} 个测试用例，开始执行验证..."

        # 5. 并发执行测试（按风险策略：模式/通道/范围/并发/max_turns）
        task_logger.info("开始执行测试用例验证")
        executor = TestExecutor(task_logger)
        test_results = executor.execute_all(
            test_cases=test_cases,
            source_path=project_path,
            diff_context=diff_info.full_diff,
            strategy=strat
        )
        task_logger.info(f"测试执行完成，共 {len(test_results)} 个结果")

        # 6. 汇总报告
        pass_count = sum(1 for r in test_results if r.success)
        fail_count = sum(1 for r in test_results if not r.success and r.result == '失败')
        error_count = sum(1 for r in test_results if r.result in ('异常', '解析失败', '解析错误'))

        summary = {
            "total": len(test_results),
            "pass": pass_count,
            "fail": fail_count,
            "error": error_count,
            "pass_rate": f"{pass_count / len(test_results) * 100:.1f}%" if test_results else "0%",
            # 风险分级驱动的执行策略，随报告落盘（可观测：事后能查每次任务用的什么策略）
            "risk_level": level,
            "strategy": {
                "analysis_mode": strat.get("analysis_mode"),
                "llm_mode": strat.get("llm_mode"),
                "scope": strat.get("scope"),
                "concurrency": strat.get("concurrency"),
                "max_turns": strat.get("max_turns"),
                "escalated": bool(strat.get("escalated"))
            }
        }

        # ---- 风险用例处理：提取相关代码段入报告 + 判定是否推送微信 ----
        push_cfg = config.get("wechat_push", {}) or {}
        min_risk = push_cfg.get("min_risk", "中") if isinstance(push_cfg, dict) else "中"
        risky_results = [r for r in test_results
                         if is_risky(getattr(r, "risk_level", "未知"), min_risk)]

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].test_results = [r.to_dict() for r in test_results]
                _tasks[task_id].summary = summary
                _tasks[task_id].status = "completed"
                _tasks[task_id].progress = "全部完成"
                # 有风险用例 → 把相关代码段一并写入报告（随轮询接口返回）
                if risky_results:
                    try:
                        target_files = (risk_assessment.high_risk_files or []) \
                            or (diff_info.changed_files or [])
                        _tasks[task_id].risk_code = extract_risk_snippets(
                            diff_info.full_diff, target_files,
                            max_files=int(push_cfg.get("max_snippets", 3) or 3),
                            max_chars=int(push_cfg.get("snippet_max_chars", 1500) or 1500))
                    except Exception as snip_err:
                        task_logger.warning(f"风险代码段提取失败（不影响任务）：{snip_err}")

        # 持久化
        with _tasks_lock:
            if task_id in _tasks:
                _save_task(task_id, _tasks[task_id])

        task_logger.info(f"任务完成：{summary}")

        # ---- 微信推送：后台线程执行，失败不影响任务结果 ----
        if risky_results:
            snapshot = {
                "task_id": task_id,
                "project_name": project_name,
                "repository_url": repository_url,
                "test_case_file": test_case_file,
                "test_results": [r.to_dict() for r in test_results],
                "summary": summary,
                "risk_assessment": risk_assessment.to_dict(),
                "commit": {
                    "before": str(diff_info.commit_hash_before)[:8],
                    "after": str(diff_info.commit_hash_after)[:8],
                },
            }
            threading.Thread(
                target=_push_wechat, args=(snapshot,), daemon=True,
                name=f"wechat-{task_id}").start()

    except Exception as e:
        task_logger.error(f"任务执行失败：{e}", exc_info=True)
        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id].status = "failed"
                _tasks[task_id].error = str(e)
                _tasks[task_id].progress = f"执行失败：{e}"
                _save_task(task_id, _tasks[task_id])

    finally:
        # 释放项目占用，并接力启动排队中的最新请求（最新胜出）
        with _project_lock:
            _running_projects.pop(project_name, None)
            nxt = _pending_projects.pop(project_name, None)
        if nxt:
            _start_or_queue(
                project_name=nxt["project_name"],
                repository_url=nxt["repository_url"],
                test_case_file=nxt["test_case_file"],
                source=nxt.get("source", "manual")
            )


@api_bp.route('/api/trigger', methods=['POST'])
def trigger_task():
    """
    触发检查任务

    Request JSON:
        project_name: str      - 项目名称
        repository_url: str    - 仓库链接
        test_case_file: str    - 测试用例文件名（位于 test_cases/ 目录）

    Returns:
        {"task_id": str, "status": "pending"}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "请求体不能为空，需要 JSON 格式"}), 400

    project_name = data.get('project_name', '').strip()
    repository_url = data.get('repository_url', '').strip()
    test_case_file = data.get('test_case_file', '').strip()

    # 参数校验
    if not project_name:
        return jsonify({"error": "缺少参数：project_name"}), 400
    if not repository_url:
        return jsonify({"error": "缺少参数：repository_url"}), 400
    if not test_case_file:
        return jsonify({"error": "缺少参数：test_case_file"}), 400

    # 校验测试用例文件是否存在
    test_cases_path = os.path.join(config.test_cases_dir, test_case_file)
    if not os.path.exists(test_cases_path):
        available = os.listdir(config.test_cases_dir) if os.path.exists(config.test_cases_dir) else []
        return jsonify({
            "error": f"测试用例文件不存在：{test_case_file}",
            "available_files": available
        }), 400

    # 创建任务（统一入口：项目在跑则排队，最新胜出）
    task_id, status = _start_or_queue(
        project_name=project_name,
        repository_url=repository_url,
        test_case_file=test_case_file,
        source="manual"
    )

    if status == "queued":
        return jsonify({
            "task_id": task_id,
            "status": "queued",
            "message": "该项目已有任务在执行，本次请求已排队（最新胜出），task_id 为当前运行中的任务"
        }), 202

    return jsonify({
        "task_id": task_id,
        "status": "pending",
        "message": "任务已创建，请使用 GET /api/task/{task_id} 查询进度和结果"
    }), 202


@api_bp.route('/api/task/<task_id>', methods=['GET'])
def get_task(task_id: str):
    """
    查询任务状态和结果

    Returns:
        TaskReport JSON
    """
    report = _get_task(task_id)
    if not report:
        return jsonify({"error": f"任务不存在：{task_id}"}), 404

    return jsonify({
        "task_id": task_id,
        **report.to_dict()
    })


@api_bp.route('/api/tasks', methods=['GET'])
def list_tasks():
    """
    列出所有任务

    Returns:
        {"tasks": [{"task_id": str, "project_name": str, "status": str, ...}]}
    """
    tasks_dir = _get_tasks_dir()
    task_list = []

    # 从内存中获取
    with _tasks_lock:
        for task_id, report in _tasks.items():
            task_list.append({
                "task_id": task_id,
                "project_name": report.project_name,
                "status": report.status,
                "progress": report.progress
            })

    # 从文件中补充（不在内存中的）
    if os.path.exists(tasks_dir):
        for filename in os.listdir(tasks_dir):
            if filename.endswith('.json'):
                task_id = filename[:-5]
                if not any(t['task_id'] == task_id for t in task_list):
                    report = _load_task(task_id)
                    if report:
                        task_list.append({
                            "task_id": task_id,
                            "project_name": report.project_name,
                            "status": report.status,
                            "progress": report.progress
                        })

    # 按状态排序：running > pending > completed > failed
    status_order = {"running": 0, "pending": 1, "completed": 2, "failed": 3}
    task_list.sort(key=lambda t: status_order.get(t.get('status', ''), 99))

    return jsonify({"tasks": task_list})
