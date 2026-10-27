#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信推送通知模块（Server酱 Turbo）

- 触发口径：任务完成后，存在「用例级风险等级 >= min_risk（默认中）」的用例时推送一条汇总
- 全部失败路径 try/except 兜底：推送挂了只记日志，绝不影响判定主流程
- 幂等：每次任务收尾只推一条；全绿任务不推（顺带节省免费额度）
- 配置见 config.json 的 wechat_push 节：
    {"enabled": true, "serverchan_key": "SCTxxxx", "min_risk": "中",
     "max_snippets": 3, "snippet_max_chars": 1500}
"""

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from checker.core.config import config

# 风险等级排序（未知排在低于中：信息不足以打扰）
_RISK_ORDER = {"低": 0, "未知": 1, "中": 2, "高": 3}
_PUSH_URL = "https://sctapi.ftqq.com/{key}.send"


def _push_cfg() -> dict:
    cfg = config.get("wechat_push", {}) or {}
    return cfg if isinstance(cfg, dict) else {}


def is_risky(risk_level: str, min_risk: str = "中") -> bool:
    """风险等级是否达到触发阈值（默认：中及以上；未知不触发）"""
    return _RISK_ORDER.get(str(risk_level), 1) >= _RISK_ORDER.get(str(min_risk), 2)


def risky_cases(test_results: List[dict], min_risk: str = "中") -> List[dict]:
    """筛出达到触发阈值的用例结果"""
    return [r for r in (test_results or [])
            if is_risky(r.get("risk_level", "未知"), min_risk)]


def extract_risk_snippets(full_diff: str, target_files: Optional[List[str]] = None,
                          max_files: int = 3, max_chars: int = 1500) -> List[Dict[str, str]]:
    """
    从 git diff 原文提取目标文件的变更代码段。

    - 按 "diff --git" 切分文件块，取 +++ 行的 b/ 路径作为文件名
    - target_files（如风险评估的高风险文件）命中优先；
      一个都没命中时回退取 diff 顺序的前 max_files 个文件块
    - 每段截断到 max_chars，防止结果 JSON 膨胀
    """
    if not full_diff or not str(full_diff).strip():
        return []
    blocks = re.split(r"(?m)^diff --git ", str(full_diff))
    picked: List[Dict[str, str]] = []

    def _pick(blk: str):
        m = re.search(r"\+\+\+ b/(\S+)", blk)
        fname = m.group(1) if m else "(未知文件)"
        body = "diff --git " + blk  # 还原前缀，保持片段可直接阅读
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…（已截断）"
        picked.append({"file": fname, "code": body})

    targets = [str(f).strip() for f in (target_files or []) if str(f).strip()]
    for blk in blocks[1:]:
        if len(picked) >= max_files:
            break
        m = re.search(r"\+\+\+ b/(\S+)", blk)
        fname = m.group(1) if m else ""
        if targets and fname not in targets:
            continue
        _pick(blk)

    # 目标文件一个都没匹配上 → 回退：按 diff 顺序取前 max_files 段
    if targets and not picked:
        for blk in blocks[1:][:max_files]:
            _pick(blk)

    return picked


def _post_serverchan(send_key: str, title: str, desp: str, logger: logging.Logger) -> bool:
    """调用 Server酱 Turbo 接口，成功返回 True；任何异常只记日志"""
    url = _PUSH_URL.format(key=send_key)
    try:
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
        if payload.get("code") == 0:
            logger.info(f"Server酱推送成功：{title}")
            return True
        logger.warning(f"Server酱推送返回异常：{payload}")
        return False
    except Exception as e:
        logger.warning(f"Server酱推送失败（网络/接口）：{e}")
        return False


def notify_task_result(task_id: str, project_name: str, repository_url: str,
                       test_case_file: str, test_results: List[dict],
                       summary: Optional[dict], risk_assessment: Optional[dict],
                       commit: Optional[dict] = None,
                       logger: Optional[logging.Logger] = None) -> bool:
    """
    任务收尾时调用（建议放后台线程）：有风险用例则推送一条微信汇总。

    Args:
        test_results: 用例结果 dict 列表（含 risk_level 字段）
        summary / risk_assessment / commit: 轮询报告同款数据
    Returns:
        是否实际推送成功（未启用/未配置 key/无风险用例均返回 False）
    """
    logger = logger or logging.getLogger("Notifier")
    try:
        cfg = _push_cfg()
        if not cfg.get("enabled", False):
            return False
        send_key = str(cfg.get("serverchan_key", "") or "").strip()
        if not send_key:
            logger.debug("wechat_push.serverchan_key 未配置，跳过推送")
            return False

        min_risk = cfg.get("min_risk", "中")
        risky = risky_cases(test_results, min_risk)
        if not risky:
            return False  # 全绿不打扰

        s = summary or {}
        title = f"【AICheckCode】{project_name}：{s.get('fail', 0)}失败/{len(risky)}风险用例"[:100]

        lines = [
            f"**项目**：{project_name}",
            f"**用例文件**：{test_case_file}",
        ]
        if commit:
            lines.append(f"**commit**：{commit.get('before', '?')} → {commit.get('after', '?')}")
        lines.append(f"**汇总**：总计 {s.get('total', '?')} | 通过 {s.get('pass', '?')} | "
                     f"失败 {s.get('fail', '?')} | 通过率 {s.get('pass_rate', '?')}")

        ra = risk_assessment or {}
        if ra.get("level"):
            lines.append(f"**diff 风险**：{ra.get('level')}（{ra.get('score', '?')} 分）")

        lines.append("")
        lines.append(f"**风险用例（{min_risk}及以上，共 {len(risky)} 条）**")
        for r in risky[:5]:
            reason = str(r.get("validation_reason", "")).replace("\n", " ")
            lines.append(f"- **{r.get('case_id', '?')}**（{r.get('risk_level', '?')} · "
                         f"{r.get('result', '?')}）：{str(r.get('description', ''))[:40]} —— {reason[:80]}")
        if len(risky) > 5:
            lines.append(f"- …另有 {len(risky) - 5} 条，详见轮询结果")

        hr_files = [str(f) for f in (ra.get("high_risk_files") or []) if str(f).strip()]
        if hr_files:
            lines.append("")
            lines.append("**高风险文件**")
            for f in hr_files[:5]:
                lines.append(f"- {f}")

        ok = _post_serverchan(send_key, title, "\n".join(lines), logger)
        if ok:
            logger.info(f"[{task_id}] 微信通知已推送：{title}")
        return ok
    except Exception as e:
        logger.warning(f"微信推送异常（不影响任务）：{e}")
        return False
