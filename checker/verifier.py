#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
功能验证服务
使用 claude_agent_sdk 调用 Claude Code
支持功能验证和风险分析
"""

import os
import re
import json
import logging
from typing import Dict, Any, Optional

try:
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage
    from claude_agent_sdk.types import TextBlock
except ImportError as e:
    raise ImportError("请安装 claude_agent_sdk: pip install claude-agent-sdk") from e

from checker.core.config import config


# ---------- inline 模式相关配置 ----------
# inline 模式：由 Python 读取源码并直接拼进 prompt，模型不需要调用任何工具。
# 目的：便宜模型（如 DeepSeek-V4-Flash）的 tool-calling 能力不足，
#       在 Agent 模式下会退化成「用自然语言假装调用工具」并陷入输出死循环
#       （实测单条输出 6.9 万 ~ 11.8 万字符，max_turns 无法终止）。
#       去掉工具依赖后，同一个模型 1~5 秒即可给出正确结论。
# 关于 .xml：MyBatis 项目的 SQL 写在 *Mapper.xml 里，加入白名单能让模型看到 SQL。
#   但实测（vhr）：加入后源码 258.7KB → 372.2KB（+44% token），且因下面
#   _collect_source 是「小文件优先 + 贪心装填」，XML 会把 Employee.java 这类
#   大文件挤出配额（丢 5 个）。若要启用 .xml，必须同时把 max_total_bytes 提到 400。
#   当前权衡：不采 .xml，改为不出「依赖 SQL 细节才能判定」的用例。
SOURCE_EXT = {".py", ".html", ".jinja", ".jinja2", ".vue", ".js",
              ".ts", ".tsx", ".jsx", ".java"}
EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    "site-packages", "static", "build", "dist", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", "migrations", "locale",
}
MAX_FILE_BYTES = 40 * 1024      # 单文件上限，超过则跳过
MAX_TOTAL_BYTES = 150 * 1024    # 兜底默认值；实际值来自 config.json 的 max_total_bytes（KB）
NO_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Bash", "Task",
    "WebSearch", "WebFetch", "TodoWrite", "NotebookEdit",
]


class FeatureVerifier:
    """功能验证器 - 也用于风险评估"""

    def __init__(self):
        self.allowed_root_paths = config.allowed_root_paths
        self.timeout = config.timeout
        self.analysis_mode = str(config.get("analysis_mode", "inline")).lower()
        # 源码采集总量上限（config.json 里以 KB 为单位，缺省 150KB）
        # 换成大项目时不用改代码，改配置即可
        self.max_total_bytes = int(config.get("max_total_bytes", 150)) * 1024
        # diff 进入 prompt 的字符预算（原实现硬编码 3000，且从头截断）
        # 智能截断会按文件分组，保证每个变更文件都保留片段
        self.diff_max_chars = int(config.get("diff_max_chars", 20000))
        self.llm_mode = str(config.get("llm_mode", "sdk")).lower()
        self.ds_cfg = config.get("deepseek", {}) or {}
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("FeatureVerifier")
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def _is_path_allowed(self, path: str) -> bool:
        """校验路径是否在允许范围内"""
        real_path = os.path.realpath(path)
        return any(real_path.startswith(root) for root in self.allowed_root_paths)

    def _collect_source(self, source_path: str, max_total: int = None,
                        changed_files: list = None):
        """收集项目源码内容（变更文件优先，再小文件优先，受总量上限约束）

        Args:
            source_path: 项目根目录
            max_total: 总量上限（字节）。缺省时取实例属性 self.max_total_bytes，
                       即 config.json 的 max_total_bytes（KB）× 1024。
                       显式传值时按传入值走（保留直接调用的可控性）。
            changed_files: 本次 diff 涉及的变更文件（相对仓库根的路径）。
                       非空时这些文件优先装填，不会被贪心排序挤掉；
                       为空时行为与改造前完全一致（保证无 diff 场景结果不变）。

        Returns:
            ([(相对路径, 文件内容), ...], 总字节数)
        """
        if max_total is None:
            max_total = self.max_total_bytes

        # 归一化变更文件路径，便于与候选文件的相对路径直接比对
        changed = set()
        for cf in (changed_files or []):
            if not cf:
                continue
            p = str(cf).replace("\\", "/")
            if p.startswith("./"):
                p = p[2:]
            changed.add(p)

        candidates = []
        for dirpath, dirnames, filenames in os.walk(source_path):
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDE_DIRS and not d.startswith(".")
            ]
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                if ext not in SOURCE_EXT:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if size == 0 or size > MAX_FILE_BYTES:
                    continue
                rel = os.path.relpath(full, source_path).replace("\\", "/")
                candidates.append((size, full, rel))

        # 装填顺序：变更文件优先（组内小文件优先），其余再按小文件优先
        if changed:
            prio = sorted([c for c in candidates if c[2] in changed],
                          key=lambda x: x[0])
            rest = sorted([c for c in candidates if c[2] not in changed],
                          key=lambda x: x[0])
            ordered = prio + rest
            if prio:
                self.logger.info(
                    "diff-aware 采集：变更文件优先装填 %d 个（候选 %d 个）",
                    len(prio), len(candidates))
        else:
            ordered = sorted(candidates, key=lambda x: x[0])

        collected, total = [], 0
        skipped = 0
        prio_missed = 0
        for size, full, rel in ordered:
            if total + size > max_total:
                skipped += 1
                if rel in changed:
                    prio_missed += 1
                continue      # 装不下就跳过，继续尝试后面的（可能更小）
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            collected.append((rel, text))
            total += size

        if skipped:
            # 改造前此处是静默 break，无日志无提示，模型也不知道源码不完整
            msg = ("源码采集触顶：上限 %d 字节，已采集 %d 文件 / %d 字节，"
                   "另有 %d 个文件未纳入" % (max_total, len(collected), total,
                                        skipped))
            if prio_missed:
                msg += "，其中变更文件 %d 个" % prio_missed
            self.logger.warning(msg + "（模型看到的是不完整源码）")
        return collected, total

    @staticmethod
    def _extract_changed_files(diff: str) -> list:
        """从 diff 文本中提取变更文件路径（用于 diff-aware 采集）"""
        if not diff:
            return []
        out = []
        for m in re.finditer(r'(?m)^diff --git a/(.+?) b/', diff):
            out.append(m.group(1))
        return out

    def _truncate_diff(self, diff: str, max_chars: int = None) -> str:
        """按文件分组智能截断 diff

        原实现是 diff[:3000]，从头截取 —— 一旦变更较大，后半部分文件全部丢失，
        且单 commit 场景下截出来的往往只是 .gitignore / README（实测确认）。
        改为按 "diff --git" 分组、每组均分预算，保证每个变更文件都保留片段。
        """
        if not diff:
            return ""
        if max_chars is None:
            max_chars = self.diff_max_chars
        if len(diff) <= max_chars:
            return diff

        parts = [p for p in re.split(r'(?m)^(?=diff --git )', diff) if p.strip()]
        if len(parts) <= 1:
            return diff[:max_chars] + "\n... [diff 已截断]"

        # 每个文件至少给 MIN_PER_FILE 字符才有阅读价值。文件过多时，
        # 与其把预算均摊成每人两百字符（等于全废），不如保证前 N 个文件
        # 有可读片段，其余只在末尾列出文件名。
        MIN_PER_FILE = 800
        can_cover = max(max_chars // MIN_PER_FILE, 1)
        if len(parts) > can_cover:
            shown_parts = parts[:can_cover]
            rest_names = []
            for p in parts[can_cover:]:
                m = re.search(r'(?m)^diff --git a/(.+?) b/', p)
                if m:
                    rest_names.append(m.group(1))
        else:
            shown_parts = parts
            rest_names = []

        per = max(max_chars // len(shown_parts), MIN_PER_FILE)
        out, used = [], 0
        for p in shown_parts:
            if used >= max_chars:
                break
            if len(p) <= per:
                out.append(p if p.endswith("\n") else p + "\n")
                used += len(p)
            else:
                # 保留文件头（含 --- / +++ 路径行）+ 正文片段；
                # 末尾补换行，保证下一个文件的 "diff --git" 仍处于行首
                head = "\n".join(p.split("\n")[:4])
                body = p[len(head):len(head) + max(per - len(head), 200)]
                out.append(head + "\n" + body
                           + "\n... [该文件 diff 片段已截断]\n")
                used += per

        tail = ""
        if rest_names:
            tail = ("\n... [变更文件过多，共 %d 个：以上展示前 %d 个，"
                    "其余 %d 个仅列文件名\n%s]\n"
                    % (len(parts), len(shown_parts), len(rest_names),
                       "\n".join("  " + n for n in rest_names[:150])))
        return "".join(out) + tail

    def _build_inline_prompt(self, source_path: str, feature_desc: str,
                             diff_context: str = "", case_id: str = "",
                             focus_files: list = None) -> str:
        """构建 inline 模式 prompt：源码直接内嵌，模型无需调用工具"""
        # diff-aware：diff 提取的变更文件 + 风险评估的重点文件合并为采集优先集，
        # 保证聚焦内容不被小文件贪心排序挤掉
        changed = set(self._extract_changed_files(diff_context))
        for ff in (focus_files or []):
            if not ff:
                continue
            p = str(ff).replace("\\", "/")
            changed.add(p[2:] if p.startswith("./") else p)
        files, total = self._collect_source(source_path,
                                            changed_files=sorted(changed))
        blocks = [f"----- 文件: {rel} -----\n{text}" for rel, text in files]

        # 触顶时告知模型源码不完整（改造前模型会误以为看到了全貌）
        truncated_note = ""
        if self.max_total_bytes and total >= self.max_total_bytes * 0.98:
            truncated_note = "\n注意：受体积上限限制，以上为项目部分源码，可能不完整。\n"

        diff_section = ""
        if diff_context:
            diff_section = (
                "\n本次代码变更内容（diff）：\n"
                f"{self._truncate_diff(diff_context)}\n"
                "\n注意：diff 只是重点线索——若待验证功能与本次变更相关，"
                "请优先依据变更后的代码判断；若与本次变更无关，"
                "请依据上面给出的项目源码判断该功能是否已实现，"
                "不要因「与本次变更无关」而直接判定为否。\n"
            )

        id_section = ""
        if case_id:
            id_section = f"\n本条任务的用例编号是：{case_id}\n"

        return f"""下面是项目 {source_path} 的核心源码，共 {len(files)} 个文件、{total} 字节。

===== 源码开始 =====
{chr(10).join(blocks)}
===== 源码结束 =====
{truncated_note}
重要约束：
1. 只依据上面给出的源码做判断，不要假设源码之外还存在其他实现。
2. 如果源码中找不到该功能的实现（没有对应路由、模型字段、表单或配置），就判定为“否”，不要因为“这类系统通常会有”而推测它存在。
3. 不要调用任何工具，直接分析并给出结论。

需要验证的功能描述：
{feature_desc}
{id_section}{diff_section}
请只输出一个 JSON 对象，不要包含任何额外文字，不要使用 markdown 代码块：
{{"用例编号": "上面给出的用例编号，原样回传", "功能是否完成": "是" 或 "否", "风险等级": "高" 或 "中" 或 "低", "校验理由": "详细说明代码中是否实现了该功能，给出具体证据（如文件名、函数名、类名、关键代码）"}}
其中"风险等级"表示该判定结果的影响程度：判定为"否"时，核心业务功能缺失填"高"，一般功能缺失填"中"，边缘或轻微问题填"低"；判定为"是"时一律填"低"。
"""

    def _build_feature_prompt(self, source_path: str, feature_desc: str,
                              diff_context: str = "", case_id: str = "",
                              mode: str = None, scope: str = None,
                              focus_files: list = None,
                              max_turns: int = None) -> str:
        """构建功能验证 prompt（按 mode 分流：inline / agent，来自风险策略）"""
        if (mode or self.analysis_mode) == "inline":
            return self._build_inline_prompt(source_path, feature_desc,
                                             diff_context, case_id,
                                             focus_files=focus_files)
        return self._build_agent_prompt(source_path, feature_desc,
                                        diff_context, case_id,
                                        scope=scope, focus_files=focus_files,
                                        max_turns=max_turns)

    def _build_agent_prompt(self, source_path: str, feature_desc: str,
                            diff_context: str = "", case_id: str = "",
                            scope: str = None, focus_files: list = None,
                            max_turns: int = None) -> str:
        """构建 agent 模式 prompt：模型用 Read/Glob/Grep 自主读码

        scope 决定探索范围的指令约束（风险分级驱动）：
        - diff_related（中风险）：优先变更/风险文件及其直接依赖
        - full（高风险）：允许全项目自由搜索
        """
        diff_section = ""
        if diff_context:
            diff_section = (
                "\n本次代码变更内容（diff）：\n"
                f"{self._truncate_diff(diff_context)}\n"
                "\n注意：上面的 diff 只是重点线索——若待验证功能与本次变更相关，"
                "请优先检查变更后的代码；若待验证功能与本次变更无关，"
                "请在项目源码中查找该功能的既有实现并据实判断，"
                "不要因「与本次变更无关」而直接判定为否。\n"
            )

        # 聚焦文件集合：diff 提取 + 风险评估给出的重点文件
        focus = sorted(set(self._extract_changed_files(diff_context))
                       | {str(f).replace("\\", "/")
                          for f in (focus_files or []) if f})
        if scope == "full":
            scope_section = (
                "探索范围：你可以在整个项目中自由搜索——路由、模型、表单、"
                "模板、配置等都可以查看，请全面判断该功能是否实现。")
        else:
            # diff_related（默认）：聚焦变更文件及直接依赖
            file_list = ("\n".join("  " + f for f in focus[:80])
                         if focus else "  （本次无变更文件信息）")
            scope_section = (
                "探索范围：优先查看以下变更/风险文件及其直接依赖模块"
                "（import / 引用它们的地方）：\n" + file_list +
                "\n如仍无法得出结论，再扩大搜索范围。")

        id_section = ""
        if case_id:
            id_section = f"\n本条任务的用例编号是：{case_id}\n"

        # 轮数预算提示：实测反例用例（证明「未实现」）和跨文件安全链路
        # 用例容易把轮数耗尽（Reached maximum turns）后整体判异常。
        # 明示预算 + 引导先看 diff 文件、证据足够即收尾，可显著减少空转。
        turns_section = ""
        if max_turns:
            turns_section = (
                f"\n重要：你的探索轮数上限是 {max_turns} 轮，请合理分配——\n"
                "1. 先读上面列出的变更/风险文件，大多数问题在这里就有答案；\n"
                "2. 用 Grep 精准定位（搜类名/路由/注解），不要逐个目录浏览；\n"
                f"3. 证据足够（能指出具体文件+方法+关键代码）就立即输出结论，"
                "不要为了「更全面」继续探索。若快到 {max_turns} 轮仍无定论，"
                "基于已有证据给出判断。\n"
            )

        return f"""请分析以下源码路径中的代码：
路径：{source_path}

{scope_section}{turns_section}
需要验证的功能描述：
{feature_desc}
{id_section}{diff_section}
请严格按照以下 JSON 格式输出分析结果，不要包含任何额外文字，不要使用 markdown 代码块：
{{"用例编号": "上面给出的用例编号，原样回传", "功能是否完成": "是" 或 "否", "风险等级": "高" 或 "中" 或 "低", "校验理由": "详细说明代码中是否实现了该功能，给出具体证据（如文件名、函数名、类名、关键代码）"}}
其中"风险等级"表示该判定结果的影响程度：判定为"否"时，核心业务功能缺失填"高"，一般功能缺失填"中"，边缘或轻微问题填"低"；判定为"是"时一律填"低"。
"""

    def _build_risk_prompt(self, diff_content: str) -> str:
        """构建风险评估 prompt"""
        return f"""请分析以下代码变更内容（git diff），评估此次变更的风险等级。

变更内容：
{self._truncate_diff(diff_content)}

请从以下几个方面评估风险：
1. 变更范围：修改了哪些模块，影响范围有多大
2. 变更类型：新增功能、修改逻辑、删除代码、重构等
3. 潜在影响：是否涉及核心逻辑、数据库、API 接口、安全相关
4. 代码质量：是否存在明显的问题或隐患

请严格按照以下 JSON 格式输出分析结果，不要包含任何额外文字，不要使用 markdown 代码块：
{{"风险等级": "高" 或 "中" 或 "低", "风险分数": 0-100的整数, "风险文件": ["文件1", "文件2"], "评估理由": "详细的风险评估说明"}}
"""

    def _parse_json(self, response: str) -> Dict[str, Any]:
        """从响应中提取 JSON

        原实现取「第一个 { 到最后一个 }」，当模型输出失控（自我重复、
        或在正文里回显了 prompt 模板）时会把大量废话夹在中间导致解析失败。
        改为：按括号配对扫出所有顶层 JSON 对象，优先取最后一个含结果字段的。
        """
        try:
            # 1) markdown 代码块
            code_block_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
            match = re.search(code_block_pattern, response, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass

            # 2) 直接解析整段
            stripped = response.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass

            # 3) 扫出所有顶层 JSON 对象
            objects = []
            depth = 0
            start = -1
            for idx, ch in enumerate(response):
                if ch == "{":
                    if depth == 0:
                        start = idx
                    depth += 1
                elif ch == "}":
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and start >= 0:
                            frag = response[start:idx + 1]
                            try:
                                parsed = json.loads(frag)
                            except json.JSONDecodeError:
                                parsed = None
                            if isinstance(parsed, dict):
                                objects.append(parsed)
                            start = -1

            if objects:
                keys = ("功能是否完成", "功能是否实现", "风险等级")
                preferred = [
                    o for o in objects
                    if any(k in o for k in keys)
                ]
                return preferred[-1] if preferred else objects[-1]

            # 4) 正则兜底：模型把 Java/SQL 代码片段（含引号、花括号）写进
            #    校验理由，或输出被截断时，整段不再是合法 JSON、括号扫描
            #    也会被字符串里的不配对花括号带偏。此时只要结论字段完整
            #    出现过，就直接按字段抽取，避免整条用例浪费 4 次重试。
            status_match = re.search(
                r'"(功能是否完成|功能是否实现|是否完成|是否实现)"\s*:\s*"([^"]*)"',
                response)
            if status_match:
                norm = self._norm_status(status_match.group(2))
                if norm:
                    out = {"功能是否完成": norm}
                    # 用例编号是模型最先输出的字段，几乎不会被后面的代码片段污染，
                    # 抽到就一并回填，让 executor 的并发串号检测在这条兜底路径上仍然生效；
                    # 抽不到则留空（executor 对空编号跳过检测，判定结果不受影响）。
                    cid_match = re.search(r'"用例编号"\s*:\s*"([^"]*)"', response)
                    if cid_match:
                        out["用例编号"] = cid_match.group(1).strip()
                    reason_match = re.search(
                        r'"(校验理由|理由|判断理由)"\s*:\s*"((?:[^"\\]|\\.)*)',
                        response, re.DOTALL)
                    if reason_match:
                        # 容忍字符串未闭合（截断），并还原常见转义
                        out["校验理由"] = (reason_match.group(2)
                                              .replace('\\"', '"')
                                              .replace("\\n", "\n")[:2000])
                    return out
            risk_match = re.search(r'"风险等级"\s*:\s*"(高|中|低)"', response)
            if risk_match:
                out = {"风险等级": risk_match.group(1)}
                score = re.search(r'"风险分数"\s*:\s*(\d+)', response)
                if score:
                    out["风险分数"] = int(score.group(1))
                return out

            return {
                "error": "解析失败",
                "reason": f"未找到有效 JSON，响应：{response[:200]}..."
            }
        except json.JSONDecodeError as e:
            return {
                "error": "解析错误",
                "reason": f"JSON 解析失败：{e}"
            }

    # 模型并不总是照抄字段名：实测约 20% 概率把「功能是否完成」写成
    # 「功能是否实现」（同义替换）。不兼容的话这些结果会被判成"解析失败"，
    # 只能靠 tenacity 重试兜底 —— 白白多花一次调用、多等几十秒。
    _STATUS_KEYS = ("功能是否完成", "功能是否实现", "是否完成", "是否实现",
                    "功能已完成", "是否已实现", "result", "status")
    _REASON_KEYS = ("校验理由", "理由", "原因", "判断理由", "分析", "说明", "reason")

    @staticmethod
    def _norm_status(value: str) -> str:
        """把各种同义写法归一到 是 / 否，认不出来才判解析失败"""
        s = str(value or "").strip()
        if s in ("是", "否"):
            return s
        low = s.lower()
        if any(t in s for t in ("是", "已实现", "已完成", "存在")) or low in ("true", "yes", "y", "1"):
            return "是"
        if any(t in s for t in ("否", "未实现", "未完成", "不存在")) or low in ("false", "no", "n", "0"):
            return "否"
        return ""

    def _map_feature_result(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """将解析结果映射为功能验证标准格式"""
        if "error" in parsed:
            return {
                "功能是否完成": parsed["error"],
                "校验理由": parsed.get("reason", ""),
                "用例编号": ""
            }

        status = ""
        for k in self._STATUS_KEYS:
            if k in parsed:
                status = self._norm_status(parsed.get(k))
                if status:
                    break

        reason = ""
        for k in self._REASON_KEYS:
            if k in parsed and str(parsed.get(k, "")).strip():
                reason = str(parsed[k]).strip()
                break

        # 用例级风险等级：prompt 要求模型输出；归一到 高/中/低，
        # 缺失或无法识别时留空，由 executor 记为"未知"（不触发推送）
        risk = ""
        rv = str(parsed.get("风险等级", "")).strip()
        if rv:
            if "高" in rv:
                risk = "高"
            elif "中" in rv:
                risk = "中"
            elif "低" in rv:
                risk = "低"

        return {
            "功能是否完成": status or "解析失败",
            "校验理由": reason or "无校验理由",
            "用例编号": str(parsed.get("用例编号", "")).strip(),
            "风险等级": risk
        }

    def _map_risk_result(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """将解析结果映射为风险评估标准格式"""
        if "error" in parsed:
            return {
                "风险等级": "未知",
                "风险分数": 0,
                "风险文件": [],
                "评估理由": parsed.get("reason", "解析失败")
            }
        return {
            "风险等级": parsed.get("风险等级", "未知"),
            "风险分数": parsed.get("风险分数", 0),
            "风险文件": parsed.get("风险文件", []),
            "评估理由": parsed.get("评估理由", "无评估理由")
        }

    def _run_async_query(self, prompt: str, source_path: str,
                         mode: str = None, max_turns: int = None) -> str:
        """异步执行 Claude 查询"""
        import anyio

        # inline 模式：禁用全部工具，模型只能基于 prompt 内已给出的源码作答，
        # 从根上避免便宜模型在 Agent 模式下陷入「假装调用工具」的输出死循环。
        if (mode or self.analysis_mode) == "inline":
            options = ClaudeAgentOptions(
                allowed_tools=[],
                disallowed_tools=list(NO_TOOLS),
                permission_mode="acceptEdits",
                cwd=source_path,
                max_turns=3
            )
        else:
            # agent 模式：只开三个只读工具；max_turns 按风险策略传入（高风险放宽）
            options = ClaudeAgentOptions(
                allowed_tools=["Read", "Glob", "Grep"],
                permission_mode="acceptEdits",
                cwd=source_path,
                max_turns=int(max_turns or 5)
            )

        full_response = ""

        async def collect_response():
            nonlocal full_response
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            full_response += block.text

        anyio.run(collect_response)
        return full_response

    def _call_deepseek(self, prompt: str) -> str:
        """直连 DeepSeek 官网 API（OpenAI 兼容 /chat/completions）

        为什么绕开 claude-agent-sdk：inline 模式下模型本来就不调用任何工具，
        起 claude.exe 子进程纯属浪费（进程启动开销 + 跨线程状态隐患）。
        另外 DeepSeek 有**自动前缀缓存** —— 本项目每条用例的 prompt 前 260KB
        源码完全相同，缓存命中后输入成本与延迟都会大幅下降。
        """
        import requests

        cfg = self.ds_cfg
        api_key = os.environ.get("DEEPSEEK_API_KEY") or cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError(
                "未配置 DeepSeek API Key：填 config.json 的 deepseek.api_key，"
                "或设环境变量 DEEPSEEK_API_KEY")

        base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        payload = {
            "model": cfg.get("model", "deepseek-flash"),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            # 思考模式默认开且 effort=high；本任务只要一个短结论，关掉省时省钱
            "thinking": {"type": "enabled" if cfg.get("thinking") else "disabled"},
            "max_tokens": int(cfg.get("max_tokens", 4000)),
        }

        resp = requests.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError("DeepSeek API %s: %s"
                               % (resp.status_code, resp.text[:300]))

        data = resp.json()
        usage = data.get("usage") or {}
        finish = data["choices"][0].get("finish_reason")
        self.logger.info(
            "DeepSeek tokens: 输入 %s（缓存命中 %s / 未命中 %s）输出 %s finish=%s",
            usage.get("prompt_tokens"), usage.get("prompt_cache_hit_tokens"),
            usage.get("prompt_cache_miss_tokens"), usage.get("completion_tokens"),
            finish)
        if finish == "length":
            # 输出被 max_tokens 截断 → 必然解析失败，直接当错误抛，让 tenacity 重试
            raise RuntimeError("DeepSeek 输出被 max_tokens 截断（%s），请调大 deepseek.max_tokens"
                               % cfg.get("max_tokens", 4000))
        return (data["choices"][0]["message"].get("content") or "")

    def _call_llm(self, prompt: str, source_path: str, llm_mode: str = None,
                  mode: str = None, max_turns: int = None) -> str:
        """按 llm_mode 分流：deepseek 直连 / sdk（claude-agent-sdk）

        llm_mode / mode / max_turns 均可被风险策略按任务覆盖；
        缺省回落到 config.json 的全局配置。
        """
        if (llm_mode or self.llm_mode) == "deepseek":
            return self._call_deepseek(prompt)
        return self._run_async_query(prompt, source_path,
                                      mode=mode, max_turns=max_turns)

    def verify_feature(self, feature_desc: str, source_path: str,
                       diff_context: str = "", case_id: str = "",
                       mode: str = None, llm_mode: str = None,
                       max_turns: int = None, scope: str = None,
                       focus_files: list = None) -> Dict[str, Any]:
        """
        验证功能是否实现

        Args:
            feature_desc: 功能描述
            source_path: 源码路径
            diff_context: 可选的 diff 上下文
            case_id: 用例编号，写进 prompt 让模型原样回传，用于并发串号检测
            mode: 覆盖 analysis_mode（inline / agent），来自风险策略
            llm_mode: 覆盖 llm_mode（deepseek / sdk），来自风险策略
            max_turns: agent 模式轮数上限（高风险放宽）
            scope: agent 模式探索范围（diff_related / full）
            focus_files: 额外聚焦文件（风险评估的重点文件），采集/提示优先

        Returns:
            {"功能是否完成": str, "校验理由": str, "用例编号": str}
        """
        if not self._is_path_allowed(source_path):
            self.logger.warning(f"路径不在允许范围内：{source_path}")
            return {"功能是否完成": "否", "校验理由": f"路径不在允许范围内：{source_path}", "用例编号": case_id}

        prompt = self._build_feature_prompt(source_path, feature_desc,
                                            diff_context, case_id,
                                            mode=mode, scope=scope,
                                            focus_files=focus_files,
                                            max_turns=max_turns)
        self.logger.info(f"开始验证功能...")

        try:
            response = self._call_llm(prompt, source_path, llm_mode=llm_mode,
                                      mode=mode, max_turns=max_turns)
            # 输出失控护栏：正常结论（是/否+理由）只有几 KB；
            # 超长说明模型陷入自我重复，按异常处理交给 tenacity 重试
            if len(response) > 50000:
                raise RuntimeError(
                    f"输出超长（{len(response)} 字符），疑似模型失控")
            parsed = self._parse_json(response)
            result = self._map_feature_result(parsed)
            self.logger.info(f"功能验证完成：{result.get('功能是否完成', '未知')}")
            return result
        except Exception as e:
            self.logger.error(f"功能验证异常：{e}")
            return {"功能是否完成": "异常", "校验理由": str(e), "用例编号": case_id}

    def assess_risk(self, diff_content: str, source_path: str) -> Dict[str, Any]:
        """
        评估代码变更风险

        Args:
            diff_content: git diff 内容
            source_path: 源码路径

        Returns:
            {"风险等级": str, "风险分数": int, "风险文件": list, "评估理由": str}
        """
        if not self._is_path_allowed(source_path):
            self.logger.warning(f"路径不在允许范围内：{source_path}")
            return {"风险等级": "未知", "风险分数": 0, "风险文件": [], "评估理由": "路径不在允许范围内"}

        prompt = self._build_risk_prompt(diff_content)
        self.logger.info(f"开始风险评估...")

        try:
            response = self._call_llm(prompt, source_path)
            parsed = self._parse_json(response)
            result = self._map_risk_result(parsed)
            self.logger.info(f"风险评估完成：等级={result.get('风险等级', '未知')}")
            return result
        except Exception as e:
            self.logger.error(f"风险评估异常：{e}")
            return {"风险等级": "未知", "风险分数": 0, "风险文件": [], "评估理由": str(e)}


# 全局实例
feature_verifier = FeatureVerifier()
