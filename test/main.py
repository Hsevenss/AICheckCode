#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
测试用例执行主程序
并发执行测试用例，实时写入 Excel 结果
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from config import config
from excel_handler import ExcelHandler
from executor import TestExecutor


def setup_logger(log_file: str) -> logging.Logger:
    """
    设置日志记录器

    Args:
        log_file: 日志文件路径

    Returns:
        日志记录器
    """
    logger = logging.getLogger("TestRunner")
    logger.setLevel(logging.INFO)

    # 清除现有 handler
    logger.handlers.clear()

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # 文件处理器（带轮转）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger


def print_banner():
    """打印横幅"""
    print("=" * 60)
    print(" " * 18 + "测试用例执行器")
    print("=" * 60)


def print_config():
    """打印配置信息"""
    print(f"配置信息:")
    print(f"  并发数：{config.concurrency}")
    print(f"  最大重试次数：{config.max_retries}")
    print(f"  源码路径：{config.source_path}")
    print(f"  超时时间：{config.timeout}秒")
    print(f"  输入文件：{config.input_file}")
    print(f"  日志文件：{config.log_file}")
    print("=" * 60)


def check_environment() -> bool:
    """
    检查运行环境

    Returns:
        环境是否正常
    """
    # 检查必要的依赖
    try:
        from claude_agent_sdk import query
    except ImportError:
        print("错误：缺少依赖 claude_agent_sdk")
        print("请运行：pip install claude-agent-sdk")
        return False

    try:
        import openpyxl
    except ImportError:
        print("错误：缺少依赖 openpyxl")
        print("请运行：pip install openpyxl")
        return False

    try:
        import tenacity
    except ImportError:
        print("错误：缺少依赖 tenacity")
        print("请运行：pip install tenacity")
        return False

    # 检查输入文件
    if not os.path.exists(config.input_file):
        print(f"错误：输入文件不存在：{config.input_file}")
        return False

    # 检查源码路径
    if not os.path.exists(config.source_path):
        print(f"警告：源码路径不存在：{config.source_path}")
        print("请确认 config.json 中的 source_path 配置是否正确")

    return True


def main():
    """主函数"""
    print_banner()

    # 环境检查
    if not check_environment():
        sys.exit(1)

    print_config()

    # 设置日志
    logger = setup_logger(config.log_file)
    logger.info("测试执行器启动")

    try:
        # 加载测试用例
        logger.info(f"加载测试用例：{config.input_file}")
        excel_handler = ExcelHandler(config.input_file)
        test_cases = excel_handler.get_test_cases()
        logger.info(f"成功加载 {len(test_cases)} 个测试用例")

        print(f"\n已加载 {len(test_cases)} 个测试用例")
        print("开始执行...\n")

        # 创建执行器
        executor = TestExecutor(excel_handler, logger)

        # 执行所有测试用例
        summary = executor.execute_all(test_cases)

        # 输出最终报告
        print("\n" + "=" * 60)
        print("测试报告")
        print("=" * 60)
        print(f"总用例数：{summary['total']}")
        print(f"通过：{summary['success']} ({summary['success']/summary['total']*100:.1f}%)")
        print(f"失败：{summary['fail']}")
        print(f"异常：{summary['error']}")
        print("=" * 60)
        print(f"结果已写入：{config.input_file}")
        print(f"日志文件：{config.log_file}")

        logger.info("测试执行器退出")

        # 关闭 Excel
        excel_handler.close()

        # 返回退出码
        if summary['error'] > 0 or summary['fail'] > 0:
            sys.exit(1)
        sys.exit(0)

    except KeyboardInterrupt:
        logger.warning("用户中断执行")
        print("\n用户中断执行")
        sys.exit(130)
    except Exception as e:
        logger.error(f"主程序异常：{e}")
        print(f"\n错误：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
