#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
风险评估模块
使用 Claude Code 分析代码变更风险
"""

import logging
from typing import Optional

from checker.core.models import DiffInfo, RiskAssessment
from checker.verifier import FeatureVerifier


class RiskAnalyzer:
    """风险评估器 - 使用 Claude Code 分析 diff 内容"""

    def __init__(self):
        self.verifier = FeatureVerifier()
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("RiskAnalyzer")
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def assess(self, diff_info: DiffInfo, source_path: str) -> RiskAssessment:
        """
        对代码变更进行风险评估

        Args:
            diff_info: Diff 信息
            source_path: 源码路径

        Returns:
            RiskAssessment 对象
        """
        if not diff_info.full_diff:
            self.logger.warning("无 diff 内容，跳过风险评估")
            return RiskAssessment(
                level="低",
                score=0,
                high_risk_files=[],
                reason="无代码变更内容"
            )

        self.logger.info(f"开始风险评估（变更文件数：{len(diff_info.changed_files)}）")

        # 调用 Claude 进行 AI 风险评估
        ai_result = self.verifier.assess_risk(diff_info.full_diff, source_path)

        # 解析 AI 返回结果
        level = ai_result.get("风险等级", "低")
        # 规范化等级
        if level not in ["高", "中", "低"]:
            level = "未知"

        score = ai_result.get("风险分数", 0)
        if not isinstance(score, (int, float)) or score < 0 or score > 100:
            score = 0

        high_risk_files = ai_result.get("风险文件", [])
        if not isinstance(high_risk_files, list):
            high_risk_files = [str(high_risk_files)]

        reason = ai_result.get("评估理由", "")

        self.logger.info(f"风险评估结果：等级={level}, 分数={score}")

        return RiskAssessment(
            level=level,
            score=int(score),
            high_risk_files=high_risk_files,
            reason=reason
        )
