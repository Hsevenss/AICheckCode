#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
并发执行器模块
使用 tenacity 实现重试机制，ThreadPoolExecutor 实现并发
"""

import logging
import threading
from typing import Dict, Any, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_result,
    before_sleep_log
)

from checker.core.config import config
from checker.core.models import TestResult
from checker.verifier import FeatureVerifier


# 线程本地存储
thread_local = threading.local()


def _should_retry(result: Dict[str, Any]) -> bool:
    """判断是否需要重试（解析失败/异常时重试）"""
    status = result.get('功能是否完成', '')
    return status in ['解析失败', '解析错误', '异常']


class TestExecutor:
    """测试执行器"""

    def __init__(self, logger: logging.Logger, concurrency: Optional[int] = None):
        """
        Args:
            logger: 日志记录器
            concurrency: 并发数，缺省取 config.json 的 concurrency
        """
        self.logger = logger
        # 每个线程独享一个 FeatureVerifier：
        # verifier 持有 logger 与 SDK 调用状态，多线程共享会在高并发下互相干扰。
        self._local = threading.local()
        self.max_retries = config.max_retries
        self.concurrency = int(concurrency or config.concurrency)

    @property
    def verifier(self) -> FeatureVerifier:
        v = getattr(self._local, "verifier", None)
        if v is None:
            v = FeatureVerifier()
            self._local.verifier = v
        return v

    def _build_feature_desc(self, test_case: Dict[str, Any]) -> str:
        """构建功能描述"""
        test_point = test_case.get('测试点', '')
        operation_steps = test_case.get('操作步骤', '')
        expected_result = test_case.get('预期结果', '')

        parts = []
        if test_point:
            parts.append(f"测试点：{test_point}")
        if operation_steps:
            parts.append(f"操作步骤：{operation_steps}")
        if expected_result:
            parts.append(f"预期结果：{expected_result}")

        return '\n'.join(parts)

    def _build_retrier(self) -> Callable:
        """构建 tenacity retry 装饰器"""
        return retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_result(_should_retry),
            before_sleep=before_sleep_log(self.logger, logging.WARNING),
            reraise=False
        )

    def execute_single(self, test_case: Dict[str, Any],
                       source_path: str, diff_context: str = "",
                       strategy: Optional[Dict[str, Any]] = None) -> TestResult:
        """
        执行单个测试用例

        Args:
            test_case: 测试用例字典
            source_path: 源码路径
            diff_context: diff 上下文
            strategy: 风险策略（mode/llm_mode/scope/max_turns/focus_files）

        Returns:
            TestResult 对象
        """
        row = test_case.get('_row', 0)
        case_id = test_case.get('用例编号', f'行{row}')
        feature_desc = self._build_feature_desc(test_case)
        test_point = test_case.get('测试点', '')

        thread_local.case_id = case_id

        try:
            self.logger.info(f"[{case_id}] 开始执行：{test_point}")

            # 使用 tenacity 重试
            retrier = self._build_retrier()

            # 风险策略 → verify_feature 参数映射（analysis_mode 在 verify 侧叫 mode）
            strat = strategy or {}
            verify_kwargs = {
                k: strat[k] for k in
                ("llm_mode", "max_turns", "scope", "focus_files")
                if strat.get(k)
            }
            if strat.get("analysis_mode"):
                verify_kwargs["mode"] = strat["analysis_mode"]

            @retrier
            def do_verify():
                return self.verifier.verify_feature(
                    feature_desc, source_path, diff_context, case_id,
                    **verify_kwargs)

            result = do_verify()

            is_completed = result.get('功能是否完成', '未知')
            reason = result.get('校验理由', '无校验理由')
            # 用例级风险等级（verifier 判定输出自带，透传给结果与微信推送触发）
            risk_level = str(result.get('风险等级', '未知') or '未知')

            # ---- 并发串号软检测 ----
            # 只记录、不改变判定：判定仍按提交时的 case_id 回填，保证与串行基线一致。
            echo_id = str(result.get('用例编号', '') or '').strip()
            if echo_id and echo_id != case_id:
                self.logger.warning(
                    f"[串号检测] 提交={case_id} 模型回传={echo_id} —— 该条结果疑似串行，请复跑确认")

            # ---- inline 与标准答案不一致 → agent 单条重验（双向对称） ----
            # 漏报方向：inline 判"否" + 标准答案"是"（聚焦读取盲区，如 SecurityConfig 不在视野）
            # 误报方向：inline 判"是" + 标准答案"否"（模型幻觉，或用例过期/功能真存在）
            # 仅 inline 模式触发（agent 已有全量工具，重验无意义）；
            # 重验用与首轮完全相同的用例 prompt，标准答案不进 prompt；
            # 只验一次：agent 给出是/否则替换判定（翻案/维持都留双理由），异常则维持原判。
            std_answer = str(test_case.get('标准答案', '') or '').strip()

            mismatch = ((is_completed == '否' and std_answer == '是') or
                        (is_completed == '是' and std_answer == '否'))

            if mismatch and strat.get('analysis_mode') == 'inline':
                self.logger.info(
                    f"[{case_id}] inline 判定（{is_completed}）与标准答案"
                    f"（{std_answer}）不一致，单条升级 agent 重验")
                try:
                    result2 = self.verifier.verify_feature(
                        feature_desc, source_path, diff_context, case_id,
                        mode='agent', llm_mode='sdk', max_turns=10,
                        scope='diff_related')
                    is2 = result2.get('功能是否完成', '未知')
                    reason2 = result2.get('校验理由', '无校验理由')
                    rl2 = str(result2.get('风险等级', '') or '')
                    if rl2:
                        risk_level = rl2
                    if is2 in ('是', '否'):
                        if is2 != is_completed:
                            self.logger.info(
                                f"[{case_id}] agent 重验翻案："
                                f"{is_completed} → {is2}")
                            reason = (f"[agent重验翻案] {reason2} "
                                      f"（首轮{is_completed}理由：{reason}）")
                        else:
                            self.logger.info(
                                f"[{case_id}] agent 重验维持原判（{is2}），"
                                f"双理由留档")
                            reason = (f"{reason} "
                                      f"[agent重验仍{is2}] {reason2}")
                        is_completed = is2
                    else:
                        self.logger.warning(
                            f"[{case_id}] agent 重验返回“{is2}”，"
                            f"维持 inline 判定")
                except Exception as re_err:
                    self.logger.warning(
                        f"[{case_id}] agent 重验异常，维持 inline 判定：{re_err}")

            # 标准答案对比：反向用例（标准答案=否）答"否"=命中预期，记通过

            if is_completed == '是':
                matched = (std_answer != '否')
                test_result_str = '通过' if matched else '失败'
                success = matched
            elif is_completed == '否':
                matched = (std_answer == '否')
                test_result_str = '通过' if matched else '失败'
                success = matched
            else:
                matched = False
                test_result_str = is_completed
                success = False

            hint = '（反向用例：功能不存在，命中预期）' if (
                std_answer == '否' and is_completed == '否') else ''
            self.logger.info(f"[{case_id}] 执行完成：{test_result_str}{hint}")

            return TestResult(
                case_id=case_id,
                description=test_point,
                result=test_result_str,
                validation_reason=reason,
                success=success,
                risk_level=risk_level,
                echo_case_id=echo_id
            )

        except Exception as e:
            self.logger.error(f"[{case_id}] 执行异常：{e}")
            return TestResult(
                case_id=case_id,
                description=test_point,
                result='异常',
                validation_reason=str(e),
                success=False
            )
        finally:
            thread_local.case_id = None

    def execute_all(self, test_cases: List[Dict[str, Any]],
                    source_path: str, diff_context: str = "",
                    strategy: Optional[Dict[str, Any]] = None) -> List[TestResult]:
        """
        并发执行所有测试用例

        Args:
            test_cases: 测试用例列表
            source_path: 源码路径
            diff_context: diff 上下文
            strategy: 风险策略（高风险降并发等），来自 routes 的风险分级决策

        Returns:
            TestResult 列表
        """
        strat = strategy or {}
        # 高风险降并发（宁可慢，减少相互干扰），缺省回落 config.json 的 concurrency
        concurrency = int(strat.get("concurrency") or self.concurrency)
        total = len(test_cases)
        self.logger.info(f"开始执行测试，共 {total} 个用例，并发数：{concurrency}")
        if strat:
            self.logger.info(
                "风险策略：mode=%s, llm=%s, scope=%s, max_turns=%s%s",
                strat.get("analysis_mode"), strat.get("llm_mode"),
                strat.get("scope"), strat.get("max_turns"),
                "（已触发预算兜底升级）" if strat.get("escalated") else "")

        results = []
        completed = 0

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_case = {
                executor.submit(
                    self.execute_single, case, source_path, diff_context, strat
                ): case
                for case in test_cases
            }

            for future in as_completed(future_to_case):
                case = future_to_case[future]
                try:
                    result = future.result()
                    results.append(result)
                    completed += 1
                except Exception as e:
                    case_id = case.get('用例编号', '未知')
                    self.logger.error(f"[{case_id}] 任务执行异常：{e}")
                    results.append(TestResult(
                        case_id=case_id,
                        description=case.get('测试点', ''),
                        result='异常',
                        validation_reason=str(e),
                        success=False
                    ))
                    completed += 1

        # 按用例原始顺序排序（原实现是 O(n^2) 的列表查找，大批量时明显偏慢）
        order = {c.get('用例编号', ''): i for i, c in enumerate(test_cases)}
        results.sort(key=lambda r: order.get(r.case_id, 10 ** 9))

        # 汇总信息
        pass_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success and r.result == '失败')
        error_count = sum(1 for r in results if r.result in ('异常', '解析失败', '解析错误'))

        self.logger.info(f"测试执行完成：总计={total}, 通过={pass_count}, 失败={fail_count}, 异常={error_count}")

        return results
