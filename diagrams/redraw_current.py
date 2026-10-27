# -*- coding: utf-8 -*-
"""按当前系统状态重画流程图和架构图（含 webhook 接入/排队/双向重验）"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei']
plt.rcParams['axes.unicode_minus'] = False

C_DEV = '#6B7B8C'   # 开发环境 灰蓝
C_API = '#3E7FC1'   # 接入/接口层 蓝
C_BIZ = '#4E9950'   # 业务层 绿
C_ATM = '#B8912A'   # 原子能力层 金
C_EXT = '#A86638'   # 外部/重验 橙棕
C_DAT = '#7163A5'   # 数据/报告 紫
C_BG = '#F7F8FA'


def box(ax, x, y, w, h, title, lines, fc, fontsize=9.5, title_size=11):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle='round,pad=0.02,rounding_size=0.06',
                                facecolor=fc, edgecolor='#333333', lw=1.2, alpha=0.95))
    cx = x + w / 2
    ax.text(cx, y + h - 0.32, title, ha='center', va='center',
            fontsize=title_size, fontweight='bold', color='white')
    n = len(lines)
    if n:
        area_top, area_bot = y + h - 0.72, y + 0.14
        spacing = min(0.38, (area_top - area_bot) / n)
        for i, ln in enumerate(lines):
            yy = area_top - spacing * (i + 0.5)
            ax.text(cx, yy, ln, ha='center', va='center',
                    fontsize=fontsize, color='white')


def arrow(ax, x1, y1, x2, y2, color='#333333', lw=1.8, style='-|>'):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                mutation_scale=16, color=color, lw=lw,
                                shrinkA=0, shrinkB=0))


# ============================================================
# 图 1：流程图 flow.png
# ============================================================
fig, ax = plt.subplots(figsize=(12.5, 18))
ax.set_xlim(0, 12.5); ax.set_ylim(0, 18)
ax.axis('off'); ax.set_facecolor(C_BG)
ax.text(6.25, 17.55, 'AICheckCode 全链路流程图（webhook 自动触发版）',
        ha='center', fontsize=16, fontweight='bold')

X, W = 1.6, 7.2
CX = X + W / 2

# 主链（自上而下）
box(ax, X, 15.75, W, 0.9, '开发者 git push', ['本机提交并推送到 Gitee'], C_DEV)
box(ax, X, 14.65, W, 0.9, 'Gitee 仓库（钩子在此）', ['WebHook 配置 push 事件 → 事件发生即回调'], C_DEV)
box(ax, X, 13.55, W, 0.9, 'POST /api/webhook（带密钥）', ['http://服务器:5000/api/webhook'], C_DEV)
box(ax, X, 12.35, W, 1.0, 'waitress 监听 5000 端口（16 线程）',
    ['一直守着端口等请求 —— 这就是"监听"'], C_API)
box(ax, X, 10.3, W, 1.85, 'webhook 处理链',
    ['① 平台识别：Gitee / GitLab / Gitea（请求头区分）',
     '② 验签：令牌比对 / HMAC-SHA256（compare_digest 防时序攻击）',
     '③ URL 归一化 + 白名单注册表匹配',
     '④ 分支过滤：忽略删分支 / 平台测试事件'], C_API, fontsize=8.8)
box(ax, X, 9.15, W, 0.95, '202 立即返回 + 后台线程启动任务', ['回调方不等待 · task_id 供轮询'], C_API)
box(ax, X, 8.0, W, 0.95, 'clone_or_pull 拉取代码',
    ['已有: reset --hard + clean -fd + pull ｜ 首次: clone'], C_BIZ)
box(ax, X, 6.9, W, 0.75, '计算增量 diff', ['diff = HEAD@{1} → HEAD（reflog 位移 = 本次拉取增量）'], C_BIZ)
box(ax, X, 5.5, W, 1.2, '风险评估（LLM）',
    ['四维度: 变更范围 / 类型 / 潜在影响 / 代码质量 → 高 / 中 / 低',
     '兜底: 空 diff = 低 ｜ 等级非法 = "未知" → 按中'], C_BIZ, fontsize=8.8)

# 策略三列
box(ax, 0.7, 3.85, 3.6, 1.45, '低 → inline',
    ['focused 读码 ≤150KB', 'diff + 高风险文件 + 小文件', '并发8 · 3轮'], C_BIZ, fontsize=8.2, title_size=9.5)
box(ax, 4.45, 3.85, 3.6, 1.45, '中 → agent',
    ['diff_related 读码范围', '工具循环 ≤10 轮', '并发8'], C_BIZ, fontsize=8.2, title_size=9.5)
box(ax, 8.2, 3.85, 3.6, 1.45, '高 → agent',
    ['full 全项目读码', '并发降至 2 保稳', '≤10 轮'], C_BIZ, fontsize=8.2, title_size=9.5)
ax.text(6.25, 3.62, '兜底：低风险但聚焦文件总量 >300KB → 强制升级 agent', ha='center',
        fontsize=8.5, color='#666666')

box(ax, X, 2.45, W, 0.95, '并发验证（ThreadPoolExecutor）',
    ['threading.local 隔离 verifier ｜ 串号检测 echo_id 回传比对'], C_ATM)
box(ax, X, 1.3, W, 0.95, '判定 vs 标准答案（双向对比计分）',
    ['一致 → 计分：反向用例答"否" = 命中预期 → 通过'], C_ATM)
box(ax, X, 0.1, W, 0.95, '报告落盘 + 查询',
    ['.tasks/{task_id}.json ｜ GET /api/task/{id} ｜ watch_task 轮询'], C_DAT)

# 侧盒：排队（最新胜出）
box(ax, 9.15, 9.05, 3.1, 1.6, '排队·最新胜出',
    ['同项目在跑时:', '新请求覆盖旧排队位', '任务 finally 释放', '并自动接力下一个'], C_API,
    fontsize=8, title_size=9.5)
arrow(ax, X + W, 9.62, 9.15, 9.62, color=C_API, style='<|-|>')
ax.text(8.92, 9.82, '在跑?', fontsize=8, color=C_API, ha='right')

# 侧盒：双向重验
box(ax, 9.15, 0.95, 3.1, 1.7, 'agent 单条重验',
    ['标准答案不进 prompt', '翻案 → 替换判定', '维持 → 失败·双理由留档', '异常 → 保原判'], C_EXT,
    fontsize=7.8, title_size=9.5)
arrow(ax, X + W, 1.77, 9.15, 1.77, color=C_EXT)
ax.text(8.92, 1.97, '不一致', fontsize=8, color=C_EXT, ha='right')

# 主链箭头
pairs = [(15.75, 15.55), (14.65, 14.45), (13.55, 13.35), (12.35, 12.15),
         (10.3, 10.1), (9.15, 8.95), (8.0, 7.65), (6.9, 6.7), (5.5, 5.3),
         (3.85, 3.4), (2.45, 2.25), (1.3, 1.05)]
for y1, y2 in pairs:
    arrow(ax, CX, y1, CX, y2)

plt.tight_layout()
plt.savefig(r'd:\AITest\aitest\AICheckCode_sanitized\AICheckCode\diagrams\flow.png',
            dpi=160, bbox_inches='tight', facecolor='white')
plt.close()
print('flow.png OK')

# ============================================================
# 图 2：架构图 architecture.png（五层 · 清淡配色版）
# ============================================================
P_API, E_API = '#D9E8F5', '#5B8FBF'   # 淡蓝
P_BIZ, E_BIZ = '#DDEEDC', '#6FA06C'   # 淡绿
P_ATM, E_ATM = '#F5ECD4', '#B8912A'   # 淡金
P_EXT, E_EXT = '#F6E3D5', '#B0713F'   # 淡橙
P_DAT, E_DAT = '#E7E1F2', '#8A7BB5'   # 淡紫
TXT = '#2F3540'

fig, ax = plt.subplots(figsize=(14, 10.8))
ax.set_xlim(0, 14); ax.set_ylim(0, 10.8)
ax.axis('off'); ax.set_facecolor('white')
ax.text(7.0, 10.35, 'AICheckCode 系统架构图（五层）', ha='center',
        fontsize=16, fontweight='bold', color=TXT)

# (层名, 副标题, 层带色, 边色, y, h, [(组件名, 职责说明), ...])
layers = [
    ('接口层', 'waitress · 16 线程', P_API, E_API, 8.6, 1.25, [
        ('/api/webhook', '接收平台推送回调，\n验签后自动触发检查'),
        ('/api/trigger', '手动指定项目和\n用例文件触发'),
        ('/api/task(s)', '查询任务进度\n和完整报告'),
        ('/api/health · 轮询', '健康检查；watch_task\n每 5 秒轮询进度'),
    ]),
    ('业务组件层', 'routes.py · executor.py', P_BIZ, E_BIZ, 6.9, 1.3, [
        ('任务调度与报告', '异步启动；同项目在跑时\n排队只跑最新；结果落盘'),
        ('风险评估', 'LLM 读 diff 从四个维度\n打分，定高 / 中 / 低'),
        ('策略决策', '按风险档选择验证模式、\n读码范围、并发数'),
        ('双向重验', '判定与标准答案矛盾时，\n单条升级 agent 复核'),
    ]),
    ('原子能力层', 'git_handler · verifier.py', P_ATM, E_ATM, 5.2, 1.3, [
        ('git_handler', '拉取 / 更新代码，\n算出本次提交增量 diff'),
        ('InlineVerifier', '低风险快验：聚焦读码，\n单轮问 LLM'),
        ('AgentVerifier', '中高风险深验：模型\n自主翻代码找证据'),
        ('解析与加载', '读 xlsx 测试用例；\n容错解析 LLM 返回'),
    ]),
    ('外部依赖层', '', P_EXT, E_EXT, 3.5, 1.2, [
        ('Gitee / GitLab / Gitea', '代码托管平台，\npush 时发 webhook 通知'),
        ('DeepSeek API', '大模型服务，\n承担评估与验证'),
        ('claude-agent-sdk', '起 Claude Code 子进程，\n提供只读工具'),
    ]),
    ('数据 / 配置层', '', P_DAT, E_DAT, 1.8, 1.2, [
        ('workspaces/', '每个项目一份\n代码工作区'),
        ('.tasks/*.json', '每次任务的\n完整报告'),
        ('config.json', 'webhook 白名单\n与风险策略'),
        ('test_cases/*.xlsx', '测试用例\n与标准答案'),
    ]),
]

for name, sub, band, edge, y, h, items in layers:
    # 层背景淡色带
    ax.add_patch(FancyBboxPatch((0.3, y), 13.4, h,
                                boxstyle='round,pad=0.02,rounding_size=0.08',
                                facecolor=band, edgecolor=edge, lw=1.0, alpha=0.55))
    # 左侧层名块（稍深一档的淡色）
    ax.add_patch(FancyBboxPatch((0.55, y + 0.15), 2.3, h - 0.3,
                                boxstyle='round,pad=0.02,rounding_size=0.06',
                                facecolor=band, edgecolor=edge, lw=1.4, alpha=1.0))
    ax.text(1.7, y + h / 2 + (0.2 if sub else 0), name, ha='center', va='center',
            fontsize=13, fontweight='bold', color=edge)
    if sub:
        ax.text(1.7, y + h / 2 - 0.24, sub, ha='center', va='center',
                fontsize=7.3, color='#666666')
    # 组件格子：白底彩边深字
    n = len(items)
    area_x, area_w = 3.15, 10.2
    slot = area_w / n
    for i, (nm, desc) in enumerate(items):
        bx = area_x + i * slot
        bw = slot - 0.25
        by, bh = y + 0.16, h - 0.32
        ax.add_patch(FancyBboxPatch((bx, by), bw, bh,
                                    boxstyle='round,pad=0.02,rounding_size=0.05',
                                    facecolor='white', edgecolor=edge, lw=1.3))
        ax.text(bx + bw / 2, by + bh - 0.24, nm, ha='center', va='center',
                fontsize=10, fontweight='bold', color=edge)
        ax.text(bx + bw / 2, by + bh / 2 - 0.16, desc, ha='center', va='center',
                fontsize=8, color='#4A4A4A', linespacing=1.45)

# 主调用链箭头（层间空隙处，柔和灰）
for y_from, y_to in [(8.6, 8.2), (6.9, 6.5), (5.2, 4.7), (3.5, 3.0)]:
    arrow(ax, 7.0, y_from, 7.0, y_to, color='#9AA5B1', lw=2.0)

ax.text(7.0, 0.72, '运行环境：腾讯云 Ubuntu · venv · waitress（未装则回退 Flask dev 并告警）'
                   ' · nohup/setsid 后台 · 安全组放行 5000',
        ha='center', fontsize=9.5, color='#555555',
        bbox=dict(boxstyle='round,pad=0.4', facecolor='white', edgecolor='#BBBBBB'))

plt.tight_layout()
plt.savefig(r'd:\AITest\aitest\AICheckCode_sanitized\AICheckCode\diagrams\architecture.png',
            dpi=160, bbox_inches='tight', facecolor='white')
plt.close()
print('architecture.png OK')
