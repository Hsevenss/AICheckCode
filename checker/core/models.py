#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
统一数据模型
"""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class DiffInfo:
    """Git 变更信息"""
    changed_files: list = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    full_diff: str = ""
    commit_hash_before: str = ""
    commit_hash_after: str = ""
    summary: str = ""
    # 是否为仓库首次提交（无增量 diff，full_diff 仅含文件清单）
    is_initial: bool = False

    def to_dict(self) -> dict:
        return {
            "changed_files": self.changed_files,
            "additions": self.additions,
            "deletions": self.deletions,
            "file_count": len(self.changed_files),
            "commit_hash_before": self.commit_hash_before,
            "commit_hash_after": self.commit_hash_after,
            "summary": self.summary,
            "is_initial": self.is_initial
        }


@dataclass
class RiskAssessment:
    """风险评估结果"""
    level: str = ""       # "高" | "中" | "低"
    score: int = 0         # 0-100
    high_risk_files: list = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "score": self.score,
            "high_risk_files": self.high_risk_files,
            "reason": self.reason
        }


@dataclass
class TestResult:
    """单个测试用例执行结果"""
    case_id: str = ""
    description: str = ""
    result: str = ""        # "通过" | "失败" | "异常" | "解析失败"
    validation_reason: str = ""
    success: bool = False
    # 用例级风险等级（LLM 判定时给出）：高 | 中 | 低 | 未知
    risk_level: str = "未知"
    # 并发串号检测：模型回传的用例编号（软检测用，不影响判定逻辑）
    echo_case_id: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "description": self.description,
            "result": self.result,
            "validation_reason": self.validation_reason,
            "success": self.success,
            "risk_level": self.risk_level,
            "echo_case_id": self.echo_case_id
        }


@dataclass
class TaskReport:
    """完整任务报告"""
    project_name: str = ""
    repository_url: str = ""
    test_case_file: str = ""
    status: str = "pending"   # pending | running | completed | failed
    progress: str = ""
    diff: Optional[dict] = None
    risk_assessment: Optional[dict] = None
    test_results: list = field(default_factory=list)
    summary: Optional[dict] = None
    # 风险用例相关代码段（[{file, code}]，仅有风险用例时生成，随报告持久化）
    risk_code: Optional[list] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "project_name": self.project_name,
            "repository_url": self.repository_url,
            "test_case_file": self.test_case_file,
            "status": self.status,
            "progress": self.progress,
        }
        if self.diff:
            d["diff"] = self.diff
        if self.risk_assessment:
            d["risk_assessment"] = self.risk_assessment
        if self.test_results:
            d["test_results"] = self.test_results
        if self.summary:
            d["summary"] = self.summary
        if self.risk_code:
            d["risk_code"] = self.risk_code
        if self.error:
            d["error"] = self.error
        return d
