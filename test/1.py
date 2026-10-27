#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
功能验证服务 - 同步实现
使用 claude_agent_sdk 调用 Claude Code
"""

import json
import anyio
from typing import Dict, Any

from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage
from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock

from core.config import config
from core.logger import setup_logger

logger = setup_logger("FeatureVerifier")


class FeatureVerifier:
    """功能验证器"""

    def __init__(self):
        self.default_source_path = config.get('feature_verifier', 'default_source_path',
                                               default='/path/to/project/AiLearningPlatform')
        self.allowed_root_paths = config.get('feature_verifier', 'allowed_root_paths',
                                              default=['/path/to/project'])
        self.timeout = config.get('feature_verifier', 'timeout', default=180)

    def _is_path_allowed(self, path: str) -> bool:
        """校验路径是否在允许范围内"""
        import os
        real_path = os.path.realpath(path)
        return any(real_path.startswith(root) for root in self.allowed_root_paths)

    def _build_prompt(self, source_path: str, feature_desc: str) -> str:
        """构建 prompt"""
        return f"""请分析以下源码路径中的代码：
路径：{source_path}

需要验证的功能描述：{feature_desc}

请严格按照以下 JSON 格式输出分析结果，不要包含任何额外文字：
{{
    "功能是否完成": "是" 或 "否",
    "校验理由": "详细说明代码中是否实现了该功能和是否符合测试用例的要求，你应该关注前后端的代码，给出具体证据（如函数名、类名、关键逻辑）和这样实现的优劣势"
}}
"""

    def _parse_json(self, response: str) -> Dict[str, Any]:
        """从响应中提取 JSON"""
        try:
            start = response.find('{')
            end = response.rfind('}') + 1
            if start != -1 and end > start:
                json_str = response[start:end]
                return json.loads(json_str)
            else:
                return {
                    "功能是否完成": "解析失败",
                    "校验理由": f"未找到有效 JSON，响应：{response[:200]}..."
                }
        except json.JSONDecodeError as e:
            return {
                "功能是否完成": "解析错误",
                "校验理由": f"JSON 解析失败：{e}"
            }

    def _run_async_verify(self, source_path: str, feature_desc: str) -> Dict[str, Any]:
        """异步执行验证（内部使用）"""
        prompt = self._build_prompt(source_path, feature_desc)

        options = ClaudeAgentOptions(
            allowed_tools=["Read", "Glob", "Grep"],
            permission_mode="acceptEdits",
            cwd=source_path
        )

        full_response = ""

        async def collect_response():
            nonlocal full_response
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            full_response += block.text
                        # 忽略 ThinkingBlock 和 ToolUseBlock 的内容

        anyio.run(collect_response)

        return self._parse_json(full_response)

    def verify(self, feature_desc: str, source_path: str = None) -> Dict[str, Any]:
        """
        验证功能是否实现（同步接口）

        Args:
            feature_desc: 功能描述
            source_path: 源码路径（可选，不传则使用默认路径）

        Returns:
            验证结果
        """
        # 使用默认路径
        if source_path is None:
            source_path = self.default_source_path

        # 安全校验
        if not self._is_path_allowed(source_path):
            logger.warning(f"路径不在允许范围内：{source_path}")
            return {
                "功能是否完成": "否",
                "校验理由": f"路径不在允许范围内：{source_path}"
            }

        logger.info(f"开始验证功能：{feature_desc[:50]}... 路径：{source_path}")

        try:
            result = self._run_async_verify(source_path, feature_desc)
            logger.info(f"功能验证完成：{result.get('功能是否完成', '未知')}")
            return result

        except Exception as e:
            logger.error(f"功能验证异常：{e}")
            return {
                "功能是否完成": "异常",
                "校验理由": str(e)
            }


# 全局实例
feature_verifier = FeatureVerifier()
