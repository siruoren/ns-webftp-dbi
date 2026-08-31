import sys
import logging

from flask import Flask


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # 将 Werkzeug 访问日志和 Flask 日志输出到 stdout（默认为 stderr）
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    _wz_logger = logging.getLogger('werkzeug')
    _wz_logger.handlers = []
    _wz_logger.propagate = True

    from app.views import register_blueprints
    register_blueprints(app)

    return app


def start_background_tasks():
    """启动后台扫描和 FTP 保活"""
    from app.models.config import ConfigManager
    from app.models.scanner import FileScanManager
    from app.models.keepalive import start_all_keepalive

    cfg = ConfigManager.load()
    scan_mgr = FileScanManager.get()
    scan_interval = int(cfg.get("scan_interval", 300))
    scan_mgr.start_auto_scan(scan_interval)
    print(f"后台扫描已启动: 每 {scan_interval} 秒刷新一次")

    start_all_keepalive()
    print(f"FTP 保活已启动: {[s['name'] for s in cfg.get('ftp_servers', [])]}")
