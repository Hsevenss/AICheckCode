#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
配置管理模块
"""

import os
import json
from typing import Any

# 项目根目录
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# 默认配置
DEFAULT_CONFIG = {
    "concurrency": 5,
    "max_retries": 3,
    "timeout": 300,
    "log_level": "INFO",
    "allowed_root_paths": [
        PROJECT_ROOT,
        "/path/to/project"
    ],
    "test_cases_dir": os.path.join(PROJECT_ROOT, "test_cases"),
    "workspaces_dir": os.path.join(PROJECT_ROOT, "workspaces"),
    "host": "0.0.0.0",
    "port": 5000,
    "debug": False
}

# 配置文件路径
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")


class Config:
    """配置管理器"""

    def __init__(self):
        self._config = DEFAULT_CONFIG.copy()
        self._load_from_file()

    def _load_from_file(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
                    self._config.update(file_config)
            except (json.JSONDecodeError, IOError) as e:
                print(f"警告：读取配置文件失败：{e}，使用默认配置")

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any):
        self._config[key] = value
        self.save()

    def save(self):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self._config, f, indent=2, ensure_ascii=False)

    @property
    def concurrency(self) -> int:
        return self._config.get("concurrency", 5)

    @property
    def max_retries(self) -> int:
        return self._config.get("max_retries", 3)

    @property
    def timeout(self) -> int:
        return self._config.get("timeout", 300)

    @property
    def allowed_root_paths(self) -> list:
        return self._config.get("allowed_root_paths", [])

    @property
    def test_cases_dir(self) -> str:
        return self._config.get("test_cases_dir", os.path.join(PROJECT_ROOT, "test_cases"))

    @property
    def workspaces_dir(self) -> str:
        return self._config.get("workspaces_dir", os.path.join(PROJECT_ROOT, "workspaces"))

    @property
    def host(self) -> str:
        return self._config.get("host", "0.0.0.0")

    @property
    def port(self) -> int:
        return self._config.get("port", 5000)

    @property
    def debug(self) -> bool:
        return self._config.get("debug", False)

    @property
    def log_level(self) -> str:
        return self._config.get("log_level", "INFO")


# 全局配置实例
config = Config()
