#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
并发执行器模块
支持重试机制和异常处理
"""

import time
import logging
import threading
from typing import Dict, Any, Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import config
from verifier import FeatureVerifier


# 线程本地存储，用于存储当前线程的用例编号
thread_local = threading.local()


class CaseIdFilter(logging.Filter):
    """日志过滤器，为每个线程添加用例编号前缀"""

    def filter(self, record):
        case_id = getattr(thread_local, 'case_id', None)
        if case_id:
            record.msg = f"[{case_id}] {record.msg}"
        return True


class TestExecutor:
    """测试执行器"""

    def __init__(self, excel_handler, logger: logging.Logger):
        """
        初始化执行器

        Args:
            excel_handler: Excel 处理器实例
            logger: 日志记录器
        """
        self.excel_handler = excel_handler
        self.logger = logger
        self.verifier = FeatureVerifier()
        self.max_retries = config.max_retries
        self.concurrency = config.concurrency
        self.source_path = config.source_path

        # 统计信息
        self.total_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.error_count = 0

        # 添加用例 ID 过滤器到 logger
        self.case_id_filter = CaseIdFilter()

    def _build_feature_desc(self, test_case: Dict[str, Any]) -> str:
        """
        构建功能描述（拼接测试点、操作步骤、预期结果）

        Args:
            test_case: 测试用例字典

        Returns:
            拼接后的功能描述
        """
        test_point = test_case.get('测试点', '')
        operation_steps = test_case.get('操作步骤', '')
        expected_result = test_case.get('预期结果', '')

        # 拼接三列内容
        parts = []
        if test_point:
            parts.append(f"测试点：{test_point}")
        if operation_steps:
            parts.append(f"操作步骤：{operation_steps}")
        if expected_result:
            parts.append(f"预期结果：{expected_result}")

        return '\n'.join(parts)

    def _verify_with_retry(self, feature_desc: str, case_id: str) -> Dict[str, Any]:
        """
        带重试的验证方法

        Args:
            feature_desc: 功能描述
            case_id: 用例编号

        Returns:
            验证结果
        """
        last_error = None
        last_raw_result = None

        for attempt in range(1, self.max_retries + 1):
            try:
                self.logger.info(f"  [第 {attempt}/{self.max_retries} 次尝试] 执行验证...")
                result = self.verifier.verify(feature_desc, self.source_path)
                last_raw_result = result

                # 检查是否需要重试（解析失败等情况）
                if result.get('功能是否完成') in ['解析失败', '解析错误', '异常']:
                    self.logger.warning(f"  [第 {attempt}/{self.max_retries} 次] 结果异常：{result.get('功能是否完成')}")
                    self.logger.warning(f"  校验理由：{result.get('校验理由', '')[:500]}")
                    last_error = result
                    if attempt < self.max_retries:
                        self.logger.info(f"  等待 {attempt} 秒后重试...")
                        time.sleep(1 * attempt)  # 递增延迟
                        continue
                else:
                    self.logger.info(f"  [第 {attempt}/{self.max_retries} 次] 验证成功")
                    return result

            except Exception as e:
                last_error = {'功能是否完成': '异常', '校验理由': str(e)}
                self.logger.error(f"  [第 {attempt}/{self.max_retries} 次] 尝试异常：{e}")
                if attempt < self.max_retries:
                    self.logger.info(f"  等待 {attempt} 秒后重试...")
                    time.sleep(1 * attempt)  # 递增延迟
                    continue
                break

        # 所有重试都失败，打印详细日志
        self.logger.error(f"  !!! 所有 {self.max_retries} 次重试均失败 !!!")
        if last_raw_result:
            self.logger.error(f"  最后一次返回结果：{last_raw_result}")

        return last_error

    def execute_single(self, test_case: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行单个测试用例

        Args:
            test_case: 测试用例字典

        Returns:
            执行结果
        """
        row = test_case.get('_row', 0)
        case_id = test_case.get('用例编号', f'行{row}')
        feature_desc = self._build_feature_desc(test_case)

        # 设置当前线程的用例编号（用于日志过滤）
        thread_local.case_id = case_id

        try:
            # 打印分隔符和用例信息
            self.logger.info("")
            self.logger.info("=" * 80)
            self.logger.info(f"开始执行")
            self.logger.info(f"  测试点：{test_case.get('测试点', '')}")
            self.logger.info(f"  操作步骤：{test_case.get('操作步骤', '')}")
            self.logger.info(f"  预期结果：{test_case.get('预期结果', '')}")
            self.logger.info("=" * 80)

            # 更新 Excel 为执行中状态
            self.excel_handler.write_result(row, "执行中...", "")

            # 执行验证（带重试）
            result = self._verify_with_retry(feature_desc, case_id)

            # 提取结果
            is_completed = result.get('功能是否完成', '未知')
            reason = result.get('校验理由', '无校验理由')

            # 判断测试结果
            if is_completed == '是':
                test_result = '通过'
                self.success_count += 1
            elif is_completed == '否':
                test_result = '失败'
                self.fail_count += 1
            else:
                test_result = is_completed
                self.error_count += 1

            # 写入 Excel
            self.excel_handler.write_result(row, test_result, reason)

            # 详细记录结果
            self.logger.info(f"执行完成：{test_result}")
            self.logger.info(f"  功能是否完成：{is_completed}")
            self.logger.info(f"  校验理由：{reason}")

            # 如果是失败或异常，打印详细的功能描述供分析
            if test_result in ['失败', '异常', '解析失败', '解析错误']:
                self.logger.error(f"  !!! 失败详情 !!!")
                self.logger.error(f"  完整功能描述:\n{feature_desc}")
                if is_completed in ['解析失败', '解析错误']:
                    self.logger.error(f"  解析失败原因：{reason}")
                    self.logger.error(f"  建议：检查 Claude 返回的响应格式是否正确")

            self.logger.info("=" * 80)

            return {
                'case_id': case_id,
                'row': row,
                'result': test_result,
                'reason': reason,
                'success': test_result == '通过'
            }

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            self.logger.error(f"执行异常：{e}")
            self.logger.error(f"异常堆栈:\n{error_detail}")
            self.excel_handler.write_result(row, '异常', str(e))
            self.error_count += 1
            self.logger.info("=" * 80)

            return {
                'case_id': case_id,
                'row': row,
                'result': '异常',
                'reason': str(e),
                'success': False
            }
        finally:
            # 清除线程本地存储
            thread_local.case_id = None

    def execute_all(self, test_cases: list) -> Dict[str, Any]:
        """
        并发执行所有测试用例

        Args:
            test_cases: 测试用例列表

        Returns:
            执行汇总结果
        """
        self.total_count = len(test_cases)
        self.success_count = 0
        self.fail_count = 0
        self.error_count = 0

        self.logger.info(f"开始执行测试，共 {self.total_count} 个用例，并发数：{self.concurrency}")

        results = []

        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            # 提交所有任务
            future_to_case = {
                executor.submit(self.execute_single, case): case
                for case in test_cases
            }

            # 收集结果
            for future in as_completed(future_to_case):
                case = future_to_case[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    self.logger.error(f"[{case.get('用例编号', '未知')}] 任务执行异常：{e}")
                    results.append({
                        'case_id': case.get('用例编号', '未知'),
                        'result': '异常',
                        'reason': str(e),
                        'success': False
                    })

        # 输出汇总
        summary = {
            'total': self.total_count,
            'success': self.success_count,
            'fail': self.fail_count,
            'error': self.error_count,
            'results': results
        }

        self.logger.info("=" * 50)
        self.logger.info("测试执行完成!")
        self.logger.info(f"总计：{self.total_count}")
        self.logger.info(f"通过：{self.success_count}")
        self.logger.info(f"失败：{self.fail_count}")
        self.logger.info(f"异常：{self.error_count}")
        self.logger.info("=" * 50)

        return summary


if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("TestExecutor")

    from excel_handler import ExcelHandler
    import sys

    if len(sys.argv) > 1:
        handler = ExcelHandler(sys.argv[1])
        executor = TestExecutor(handler, logger)
        cases = handler.get_test_cases()[:3]  # 只测试前 3 个
        result = executor.execute_all(cases)
        print(result)
        handler.close()
