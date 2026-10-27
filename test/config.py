#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
配置管理模块
"""

import os
import json
from typing import Any, Optional

# 默认配置
DEFAULT_CONFIG = {
    "concurrency": 5,              # 并发数
    "max_retries": 3,              # 最大重试次数
    "source_path": "/path/to/project/AiLearningPlatform",  # 源码路径
    "timeout": 180,                # 超时时间（秒）
    "input_file": "测试用例.xlsx",
    "log_file": "test_runner.log",
    "allowed_root_paths": ["/path/to/project"]  # 允许的路径根目录
}

# 配置文件路径
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class Config:
    """配置管理器"""

    def __init__(self):
        self._config = DEFAULT_CONFIG.copy()
        self._load_from_file()

    def _load_from_file(self):
        """从配置文件加载"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
                    self._config.update(file_config)
            except (json.JSONDecodeError, IOError) as e:
                print(f"警告：读取配置文件失败：{e}，使用默认配置")

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        return self._config.get(key, default)

    def set(self, key: str, value: Any):
        """设置配置值"""
        self._config[key] = value

    def save(self):
        """保存配置到文件"""
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self._config, f, indent=2, ensure_ascii=False)

    @property
    def concurrency(self) -> int:
        return self._config.get("concurrency", 5)

    @property
    def max_retries(self) -> int:
        return self._config.get("max_retries", 3)

    @property
    def source_path(self) -> str:
        return self._config.get("source_path", DEFAULT_CONFIG["source_path"])

    @property
    def timeout(self) -> int:
        return self._config.get("timeout", 180)

    @property
    def input_file(self) -> str:
        return self._config.get("input_file", "测试用例.xlsx")

    @property
    def log_file(self) -> str:
        return self._config.get("log_file", "test_runner.log")

    @property
    def allowed_root_paths(self) -> list:
        return self._config.get("allowed_root_paths", DEFAULT_CONFIG["allowed_root_paths"])


# 全局配置实例
config = Config()


if __name__ == "__main__":
    # 测试配置
    print("当前配置:")
    print(f"  并发数：{config.concurrency}")
    print(f"  最大重试次数：{config.max_retries}")
    print(f"  源码路径：{config.source_path}")
    print(f"  超时时间：{config.timeout}")
    print(f"  输入文件：{config.input_file}")
    print(f"  日志文件：{config.log_file}")
