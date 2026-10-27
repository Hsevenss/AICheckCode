#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Git 操作模块
处理仓库的 clone、pull、diff 获取
"""

import os
import subprocess
import logging
from typing import Optional

from checker.core.config import config
from checker.core.models import DiffInfo


class GitHandler:
    """Git 操作处理器"""

    def __init__(self):
        self.workspaces_dir = config.workspaces_dir
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("GitHandler")
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def _run_git_command(self, cmd: list, cwd: str) -> str:
        """执行 git 命令并返回输出"""
        # 显式指定 utf-8：Windows 中文控制台默认 GBK，diff/log 里的
        # UTF-8 中文内容（注释、提交信息）会触发 UnicodeDecodeError，
        # 导致 reader 线程崩溃、stdout 变 None、diff 获取失败
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            raise RuntimeError(f"Git 命令执行失败：{' '.join(cmd)}\n{error_msg}")
        return result.stdout.strip()

    def get_project_path(self, project_name: str) -> str:
        """获取项目路径"""
        return os.path.join(self.workspaces_dir, project_name)

    def clone_or_pull(self, repository_url: str, project_name: str) -> str:
        """
        Clone 或 Pull 仓库

        Args:
            repository_url: 仓库 URL
            project_name: 项目名称

        Returns:
            项目本地路径
        """
        project_path = self.get_project_path(project_name)

        if os.path.exists(project_path):
            self.logger.info(f"仓库已存在，执行 git pull：{project_path}")
            self._run_git_command(["git", "reset", "--hard", "HEAD"], project_path)
            self._run_git_command(["git", "clean", "-fd"], project_path)
            pull_output = self._run_git_command(["git", "pull"], project_path)
            self.logger.info(f"Pull 完成：{pull_output}")
        else:
            self.logger.info(f"仓库不存在，执行 git clone：{repository_url}")
            parent_dir = self.workspaces_dir
            os.makedirs(parent_dir, exist_ok=True)
            self._run_git_command(
                ["git", "clone", repository_url, project_name],
                cwd=parent_dir
            )
            self.logger.info("Clone 完成")

        return project_path

    def _is_initial_commit(self, project_path: str) -> bool:
        """判断是否为初始 commit（只有一个提交）"""
        try:
            count = self._run_git_command(
                ["git", "rev-list", "--count", "HEAD"], project_path
            )
            return int(count) <= 1
        except (RuntimeError, ValueError):
            return True

    def _get_initial_diff(self, project_path: str) -> dict:
        """获取初始 commit 的信息

        ⚠️ 改造说明：原实现用 `git show HEAD` 返回**整个仓库的全部内容**，
        vhr 实测达 6.6 MB（242 个文件）。上层再截断到 3000 字符后，
        模型实际看到的只剩 .gitattributes / .gitignore / About.md / README.md，
        业务代码 0 行 —— diff 完全沦为噪声。

        单 commit 仓库本就不存在「变更」这个概念，正确做法是不产出内容级 diff，
        改为返回「新增文件清单 + 统计摘要」。
        """
        current_head = self._run_git_command(
            ["git", "rev-parse", "HEAD"], project_path
        )

        # 所有文件列表
        files_raw = self._run_git_command(
            ["git", "ls-files"], project_path
        )
        changed_files = [f.strip() for f in files_raw.split('\n') if f.strip()]

        # 统计（git show --stat），不取内容
        diff_stat = self._run_git_command(
            ["git", "show", "--stat", "HEAD"], project_path
        )

        # 统计增删行
        additions = 0
        deletions = 0
        import re
        stat_summary = diff_stat.split('\n')[-1] if diff_stat else ""
        m = re.search(r'(\d+)\s+insertion', stat_summary)
        if m:
            additions = int(m.group(1))
        m = re.search(r'(\d+)\s+deletion', stat_summary)
        if m:
            deletions = int(m.group(1))

        # 文件清单（供模型了解项目结构，比塞全量内容有效得多）
        shown = changed_files[:500]
        file_list = "\n".join("  " + f for f in shown)
        if len(changed_files) > len(shown):
            file_list += "\n  ...（其余 %d 个文件已省略）" % (
                len(changed_files) - len(shown))

        full_diff = (
            "[初始提交] 本次为仓库首次提交，没有可比较的上一个版本，"
            "因此不存在增量 diff 内容。\n"
            "本次提交共包含 %d 个文件，清单如下：\n%s\n"
            % (len(changed_files), file_list)
        )

        return {
            "changed_files": changed_files,
            "additions": additions,
            "deletions": deletions,
            "full_diff": full_diff,
            "commit_hash_before": "初始提交",
            "commit_hash_after": current_head,
            "summary": diff_stat,
            "is_initial": True
        }

    def _get_diff_between(self, project_path: str, before: str,
                          after: str) -> dict:
        """获取两个 commit 之间的增量 diff"""
        current_head = self._run_git_command(
            ["git", "rev-parse", after], project_path
        )

        # 变更文件列表
        try:
            files_raw = self._run_git_command(
                ["git", "diff", "--name-only", before, current_head],
                project_path
            )
            changed_files = [f.strip() for f in files_raw.split('\n') if f.strip()]
        except RuntimeError:
            changed_files = []

        # 统计
        try:
            diff_stat = self._run_git_command(
                ["git", "diff", "--stat", before, current_head],
                project_path
            )
        except RuntimeError:
            diff_stat = ""

        # 完整 diff
        try:
            full_diff = self._run_git_command(
                ["git", "diff", before, current_head],
                project_path
            )
        except RuntimeError:
            full_diff = ""

        # 统计增删行
        additions = 0
        deletions = 0
        import re
        for line in diff_stat.split('\n'):
            m = re.search(r'(\d+)\s+insertion', line)
            if m:
                additions = int(m.group(1))
            m = re.search(r'(\d+)\s+deletion', line)
            if m:
                deletions = int(m.group(1))

        return {
            "changed_files": changed_files,
            "additions": additions,
            "deletions": deletions,
            "full_diff": full_diff,
            "commit_hash_before": before,
            "commit_hash_after": current_head,
            "summary": diff_stat,
            "is_initial": False
        }

    def _get_incremental_diff(self, project_path: str) -> dict:
        """获取最近一次 pull 带来的增量变更（HEAD@{1} -> HEAD）"""
        current_head = self._run_git_command(
            ["git", "rev-parse", "HEAD"], project_path
        )
        try:
            previous_head = self._run_git_command(
                ["git", "rev-parse", "HEAD@{1}"], project_path
            )
        except RuntimeError:
            previous_head = current_head
        return self._get_diff_between(project_path, previous_head, current_head)

    def get_diff(self, project_path: str, base_commit: str = None) -> DiffInfo:
        """
        获取变更内容

        Args:
            project_path: 项目路径
            base_commit: 可选的基准 commit。指定时取 base_commit..HEAD 的增量，
                         使单 commit 的浅克隆仓库也能做增量对比。

        Returns:
            DiffInfo 对象
        """
        try:
            if base_commit:
                data = self._get_diff_between(project_path, base_commit, "HEAD")
            elif self._is_initial_commit(project_path):
                data = self._get_initial_diff(project_path)
            else:
                data = self._get_incremental_diff(project_path)

            return DiffInfo(
                changed_files=data["changed_files"],
                additions=data["additions"],
                deletions=data["deletions"],
                full_diff=data["full_diff"],
                commit_hash_before=data["commit_hash_before"],
                commit_hash_after=data["commit_hash_after"],
                summary=data["summary"],
                is_initial=bool(data.get("is_initial", False))
            )

        except Exception as e:
            self.logger.error(f"获取 diff 失败：{e}", exc_info=True)
            return DiffInfo(
                changed_files=[],
                additions=0,
                deletions=0,
                full_diff="",
                commit_hash_before="",
                commit_hash_after="",
                summary=""
            )
