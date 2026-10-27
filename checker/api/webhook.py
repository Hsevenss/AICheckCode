#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Webhook 接收模块（Gitee / GitLab / Gitea）

「监听」的本质：本模块不盯着任何仓库。服务进程把 5000 端口交给
操作系统监听（socket bind + listen），进程平时挂起；Git 平台收到
push 后主动 POST 到 /api/webhook，进程才被叫醒执行这里的代码。
被动等 HTTP，零轮询 —— 平台"推"事件，本服务再去"拉"代码。
"""

import os
import hmac
import hashlib
import logging

from flask import Blueprint, request, jsonify

from checker.core.config import config
from checker.api.routes import _start_or_queue

webhook_bp = Blueprint('webhook', __name__)
logger = logging.getLogger("Webhook")

_ZERO_SHA = '0' * 40  # 删分支 / 平台"测试"按钮的特征值

# 各平台 push 事件头的取值
_PUSH_EVENTS = {'gitlab': 'Push Hook', 'gitee': 'Push Hook', 'gitea': 'push'}

# 各平台"仓库地址"在 payload 里的候选字段（按优先级）
_REPO_FIELDS = {
    'gitlab': [('project', 'git_http_url'), ('project', 'http_url'),
               ('project', 'web_url'), ('repository', 'url')],
    'gitee': [('repository', 'git_http_url'), ('repository', 'url'),
              ('repository', 'html_url')],
    'gitea': [('repository', 'clone_url'), ('repository', 'html_url')],
}


def _detect_platform(headers) -> str:
    """靠平台专属请求头识别来源"""
    if 'X-Gitlab-Event' in headers:
        return 'gitlab'
    if 'X-Gitee-Event' in headers:
        return 'gitee'
    if 'X-Gitea-Event' in headers:
        return 'gitea'
    return ''


def _normalize_url(url) -> str:
    """归一化仓库地址：统一斜杠、去 .git 后缀、去尾部斜杠"""
    if not url:
        return ''
    u = str(url).strip().replace('\\', '/')
    if u.endswith('.git'):
        u = u[:-4]
    return u.rstrip('/')


def _verify(platform: str, body: bytes, headers, payload: dict):
    """验签。返回 (ok, msg)。secret 未配置时一律拒绝（不裸奔）。"""
    secrets = (config.get('webhook', {}) or {}).get('secrets', {}) or {}
    secret = str(secrets.get(platform, '') or '').strip()

    if not secret:
        return False, f'服务端未配置 {platform} 的 secret，拒绝请求'

    if platform == 'gitea':
        # Gitea：X-Gitea-Signature = HMAC-SHA256(原始请求体, secret)
        got = (headers.get('X-Gitea-Signature') or '').strip().lower()
        expected = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(got, expected):
            return False, '签名不匹配'
        return True, ''

    # Gitee / GitLab：令牌明文对比（constant-time，防时序侧信道）
    if platform == 'gitlab':
        got = headers.get('X-Gitlab-Token') or ''
    else:  # gitee：优先请求头，缺失时回退 payload 里的 password
        got = headers.get('X-Gitee-Token') or str(payload.get('password') or '')
    if not hmac.compare_digest(str(got).strip(), secret):
        return False, '令牌不匹配'
    return True, ''


def _parse_event(platform: str, headers, payload: dict) -> dict:
    """把各平台 payload 解析成统一结构 {kind, repo_url, branch, reason}"""
    event_header = headers.get(
        {'gitlab': 'X-Gitlab-Event', 'gitee': 'X-Gitee-Event',
         'gitea': 'X-Gitea-Event'}[platform], '')

    # 只处理 push，其余（tag / issue / ping 等）一律忽略
    if event_header != _PUSH_EVENTS[platform]:
        return {'kind': 'ignored', 'reason': f'事件类型 {event_header} 不是 push'}

    # 仓库地址：按候选字段找
    repo_url = ''
    for section, field in _REPO_FIELDS[platform]:
        repo_url = (payload.get(section) or {}).get(field) or ''
        if repo_url:
            break

    ref = str(payload.get('ref') or '')
    after = str(payload.get('after') or '')

    # 删分支 / 平台"测试"按钮：after 为全 0 或缺失
    if not after or set(after) == {'0'}:
        return {'kind': 'ignored', 'reason': '删除分支或平台测试请求',
                'repo_url': repo_url}

    # 只认分支推送（refs/tags/... 不触发）
    if not ref.startswith('refs/heads/'):
        return {'kind': 'ignored', 'reason': f'非分支推送 {ref}',
                'repo_url': repo_url}

    return {
        'kind': 'push',
        'repo_url': repo_url,
        'branch': ref[len('refs/heads/'):],
        'after': after,
    }


def _find_project(repo_url: str):
    """注册表白名单查找（URL 归一化后精确匹配）"""
    projects = (config.get('webhook', {}) or {}).get('projects', []) or []
    target = _normalize_url(repo_url)
    for proj in projects:
        if _normalize_url(proj.get('repo_url', '')) == target:
            return proj
    return None


@webhook_bp.route('/api/webhook', methods=['POST'])
def receive_webhook():
    # 原始字节流：Gitea 验签必须用解析前的 body
    body = request.get_data()
    payload = request.get_json(silent=True) or {}

    platform = _detect_platform(request.headers)
    if not platform:
        return jsonify({'error': '无法识别的 Webhook 来源（支持 Gitee / GitLab / Gitea）'}), 400

    ok, msg = _verify(platform, body, request.headers, payload)
    if not ok:
        logger.warning(f'Webhook 验签失败：platform={platform}, {msg}')
        return jsonify({'error': f'验签失败：{msg}'}), 401

    logger.debug(f'Webhook 原始 payload：{payload}')
    event = _parse_event(platform, request.headers, payload)
    logger.info(f'Webhook 事件：platform={platform}, kind={event["kind"]}, '
                f'repo={event.get("repo_url", "")}, reason={event.get("reason", "")}')

    if event['kind'] != 'push':
        # 非目标事件返回 200，让平台配置页显示"推送成功"
        return jsonify({'status': 'ignored', 'reason': event.get('reason', '')}), 200

    proj = _find_project(event['repo_url'])
    if not proj:
        logger.warning(f'未注册的仓库：{event["repo_url"]}')
        return jsonify({'error': f'仓库未在 webhook.projects 注册：'
                                 f'{_normalize_url(event["repo_url"])}'}), 404

    # 分支过滤：未配置 branches 则放行所有分支
    branches = proj.get('branches') or []
    if branches and event['branch'] not in branches:
        return jsonify({'status': 'ignored',
                        'reason': f'分支 {event["branch"]} 不在监听列表'}), 200

    # 校验注册表指向的用例文件（注册表可能过期）
    test_path = os.path.join(config.test_cases_dir, proj['test_case_file'])
    if not os.path.exists(test_path):
        return jsonify({'error': f'注册表指向的用例文件不存在：'
                                 f'{proj["test_case_file"]}'}), 400

    # 用注册表里的地址去 clone/pull（规范化），而非 payload 里的原始地址
    task_id, status = _start_or_queue(
        project_name=proj['project_name'],
        repository_url=proj['repo_url'],
        test_case_file=proj['test_case_file'],
        source=f'webhook:{platform}'
    )

    if status == 'queued':
        return jsonify({
            'status': 'queued',
            'message': '该项目已有任务在执行，本次请求已排队（最新胜出，稍后自动执行）'
        }), 202

    return jsonify({
        'task_id': task_id,
        'status': 'started',
        'message': '已触发检查任务，请使用 GET /api/task/{task_id} 查询进度'
    }), 202
