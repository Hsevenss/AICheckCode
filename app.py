#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Flask 应用入口
AICheckCode - AI 代码变更检查服务
"""

import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler

from flask import Flask
from flask_cors import CORS

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker.core.config import config
from checker.api.routes import api_bp
from checker.api.webhook import webhook_bp


def create_app() -> Flask:
    """创建 Flask 应用"""
    app = Flask(__name__)

    # 配置
    app.config['SECRET_KEY'] = os.urandom(24).hex()
    app.config['JSON_AS_ASCII'] = False  # 支持中文 JSON
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB

    # CORS
    CORS(app)

    # 注册蓝图
    app.register_blueprint(api_bp)
    app.register_blueprint(webhook_bp)

    # 设置日志
    _setup_logging(app)

    # 健康检查
    @app.route('/api/health', methods=['GET'])
    def health():
        return {
            "status": "ok",
            "version": "1.0.0",
            "test_cases_dir": config.test_cases_dir,
            "workspaces_dir": config.workspaces_dir
        }

    # 列出可用测试用例文件
    @app.route('/api/testcases', methods=['GET'])
    def list_test_cases():
        test_cases_dir = config.test_cases_dir
        if not os.path.exists(test_cases_dir):
            return {"test_cases": []}
        files = [f for f in os.listdir(test_cases_dir)
                 if f.endswith('.xlsx') and os.path.isfile(os.path.join(test_cases_dir, f))]
        return {"test_cases": files}

    return app


def _setup_logging(app: Flask):
    """设置日志"""
    log_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "server.log"
    )

    # 根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    # 控制台
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 文件日志（轮转）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Flask 日志
    app.logger.handlers = []
    app.logger.propagate = True

    logging.getLogger("API").setLevel(logging.INFO)
    logging.getLogger("GitHandler").setLevel(logging.INFO)
    logging.getLogger("FeatureVerifier").setLevel(logging.INFO)
    logging.getLogger("RiskAnalyzer").setLevel(logging.INFO)


def _recover_stale_tasks():
    """服务重启时，把上次没跑完的任务标记为 interrupted（否则永远卡在 running）"""
    tasks_dir = os.path.join(config.workspaces_dir, ".tasks")
    if not os.path.isdir(tasks_dir):
        return
    for filename in os.listdir(tasks_dir):
        if not filename.endswith('.json'):
            continue
        filepath = os.path.join(tasks_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('status') in ('running', 'pending'):
                data['status'] = 'interrupted'
                data['progress'] = '服务重启，任务中断'
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            continue


if __name__ == "__main__":
    app = create_app()
    _recover_stale_tasks()
    print(f"""
╔══════════════════════════════════════════════════╗
║         AICheckCode Service v1.0.0              ║
║──────────────────────────────────────────────────║
║  服务地址: http://{config.host}:{config.port}           ║
║  测试用例: {config.test_cases_dir}  ║
║  工作目录: {config.workspaces_dir}   ║
║  并发数: {config.concurrency} | 重试次数: {config.max_retries}    ║
╚══════════════════════════════════════════════════╝
    """)
    # 生产优先 waitress（Windows 友好、扛并发）；未安装则回退 Flask 自带服务器
    try:
        from waitress import serve
        print("运行模式: waitress（生产级，Webhook 入口 /api/webhook 已启用）")
        serve(app, host=config.host, port=config.port, threads=16)
    except ImportError:
        print("警告: 未安装 waitress，回退 Flask 开发服务器（生产请 pip install waitress）")
        app.run(
            host=config.host,
            port=config.port,
            debug=config.debug,
            threaded=True
        )
