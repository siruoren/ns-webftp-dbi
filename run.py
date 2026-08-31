#!/usr/bin/env python3
"""
Switch DBI FTP 传输工具 - 启动入口
自动扫描目录下的 Switch 安装包，通过 FTP 发送到 Switch 上的 DBI 后端。
"""

import os

from app import create_app, start_background_tasks
from app.models.config import ConfigManager

app = create_app()

if __name__ == "__main__":
    cfg = ConfigManager.load()
    host = os.environ.get("HOST", cfg.get("server", {}).get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT", cfg.get("server", {}).get("port", 8090)))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    start_background_tasks()

    print(f"Switch DBI FTP 传输工具启动中...")
    print(f"访问地址: http://localhost:{port}")
    print(f"扫描目录: {cfg.get('scan_dirs', [])}")
    print(f"FTP 服务器: {[s['name'] for s in cfg.get('ftp_servers', [])]}")
    # debug 模式下也禁用 reloader，避免 config.yml 变化时重启
    # threaded=True 允许并发处理请求，防止多终端刷新时请求排队影响上传
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
