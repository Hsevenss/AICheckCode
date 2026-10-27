# AICheckCode · 用例驱动的 AI 功能验证与代码审查系统

开发者在 push 代码后，系统由 Webhook 自动触发，基于 Git diff 做风险评估并分档执行，**静态判定 Excel 测试用例对应的功能是否已实现**——被测项目无需运行。高中风险自动回传代码段并推送微信告警，把问题拦截在提测之前。

## 项目背景

传统流程中，用例执行依赖提测后的测试环节：开发者提交 → 测试执行用例 → 发现问题打回，返工成本高。本项目的思路是**验证左移**：以既有测试用例为锚点，在代码 push 时刻就让 LLM 对照源码做静态功能验证，提前暴露"用例要求的功能没实现 / 实现与预期不符"的问题。

## 工作流程

```
push 代码 → Webhook 验签 → Git diff 增量分析 → 风险评估（高/中/低）
    → 分档执行策略 → AI 判定（Excel 用例 × 源码）→ 结果回传 + 微信告警
```

- 任务采用 `202 + task_id + 轮询` 异步模型，100 条用例批量判定不阻塞；
- 被测项目全程无需运行，判定依据是源码静态分析。

## 核心设计

### 1. 风险分档 + 双通道执行

| 风险档 | 通道 | 策略 |
|---|---|---|
| 低 | inline（DeepSeek 直连） | 源码预采集、focused 范围、并发 8、max_turns 3 |
| 中 | Agent（Claude Code SDK） | diff_related 范围、并发 8、max_turns 10 |
| 高 | Agent（Claude Code SDK） | full 范围、并发 2、max_turns 10，宁慢勿错 |

- **inline 通道**：小文件优先、贪心装填，采集总量受预算约束（默认 300KB）；聚焦文件超出预算时自动升级 Agent——按需读码天生适合装不下的大仓库；
- **Agent 通道**：Read / Glob / Grep 最小权限只读白名单，`max_turns` 轮次强制止损（低 3 / 中高 10），防止弱工具调用模型陷入死循环；
- 未知风险按"中"档保守兜底。

### 2. LLM 输出治理（反幻觉与解析兜底）

- **反幻觉 Prompt 三约束**：仅依据源码 / 找不到即答否 / 禁止推测；
- **三层 JSON 解析防线**：Markdown 代码块剥离 → 整体 JSON 解析 → 括号扫描回退；
- **同义字段归一化**：JSON mode 只保证语法合法，不保证语义正确（字段名同义替换、值不规范），归一化是解析成功率从 80% 到 100% 的另一半功臣；
- 输出被 max_tokens 截断时直接抛错走重试，不浪费解析机会（tenacity，默认 3 次指数退避）。

### 3. 并发工程

- ThreadPoolExecutor 多用例并发；
- 修复并发"串号"竞态：无报错、无崩溃、仅结果对不上——定位为线程共享 client 的竞争问题，用 `threading.local` 线程独享验证器修复，并追加用例编号收发一致性校验防复发；
- 3 vs 8 并发对照实验定位下游 API 瓶颈（加速 2.33×，串号 0）。

### 4. 成本优化

固定 Prompt 前缀命中 DeepSeek 自动前缀缓存，命中率 99.5%，单条判定耗时 1–5 秒。

## 量化结果

| 指标 | 结果 |
|---|---|
| JSON 解析成功率 | ~80% → **100%**（压测 10/10） |
| 复跑一致率 | **97–99%**（同输入 4 次全量复跑，误判逐一归因） |
| 前缀缓存命中率 | **99.5%** |
| 单条判定耗时 | **1–5 秒**（死循环重构前为数十秒） |
| 并发对照实验 | 3 vs 8 线程，加速 **2.33×**，串号 **0** |

## 踩坑记录

- **伪工具调用死循环**：弱工具调用模型在 Agent 模式下输出伪 tool_use 协议块，单条输出 6.9 万+ 字符无法终止。解法：inline 预采集方案 + `max_turns` 止损双保险；
- **Agent 结果解析**：Agent 是多轮消息流，最终答案可能带 Markdown 包装、引用代码片段（花括号/引号破坏 JSON 结构）、被截断——三层解析防线就是为此设计。

## 目录结构

```
AICheckCode/
├── app.py                 # Flask 入口
├── checker/
│   ├── api/               # 路由与 Webhook 验签
│   ├── core/              # 配置与数据模型
│   ├── excel_handler.py   # Excel 用例解析
│   ├── git_handler.py     # Git 操作与 diff 解析
│   ├── risk_analyzer.py   # 风险评估（触点/体量/语义三因子）
│   ├── executor.py        # 并发调度执行
│   ├── verifier.py        # LLM 判定（inline / Agent 双通道）
│   └── notifier.py        # 微信告警推送（Server酱）
├── test_cases/            # 测试用例 Excel（vhr / bluelog 两套被测项目）
├── diagrams/              # 架构图
├── config.example.json    # 配置模板
└── requirements.txt
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置
cp config.example.json config.json
# 填入你的 DeepSeek API Key、Webhook Secret、Server酱 SendKey，
# 并配置被测项目的 repo 路径与测试用例文件

# 3. 启动
python app.py   # 默认 0.0.0.0:5000
```

在被测项目的 Git 平台（Gitee / GitLab / Gitea）配置 Webhook 指向本服务地址，push 即触发。项目已部署于腾讯云 Linux 服务器运行。

## 技术栈

Python · Flask · DeepSeek API · Claude Code（Agent SDK）· ThreadPoolExecutor · tenacity · Git / Webhook · Server酱
