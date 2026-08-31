import threading
from flask import Blueprint, request, jsonify, render_template

from app.models.config import ConfigManager, DEFAULT_EXTENSIONS
from app.models.scanner import FileScanManager

bp = Blueprint("files", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/files")
def list_files():
    show_all = request.args.get("all", "false").lower() == "true"
    mgr = FileScanManager.get()
    files = mgr.get_files(show_all=show_all)
    return jsonify({
        "files": files,
        "total": len(files),
        "scanning": mgr.is_scanning,
        "last_scan_time": mgr.last_scan_time,
    })


@bp.route("/api/files/scan", methods=["POST"])
def rescan_files():
    mgr = FileScanManager.get()
    if mgr.is_scanning:
        return jsonify({
            "scanning": True,
            "message": "刷新任务进行中",
            "total": len(mgr.get_files()),
        })
    threading.Thread(target=mgr.do_scan, daemon=True).start()
    return jsonify({
        "scanning": True,
        "message": "刷新任务已启动",
        "total": len(mgr.get_files()),
        "last_scan_time": mgr.last_scan_time,
    })


@bp.route("/api/files/scan-status")
def scan_status():
    mgr = FileScanManager.get()
    return jsonify({
        "scanning": mgr.is_scanning,
        "last_scan_time": mgr.last_scan_time,
    })


@bp.route("/api/scan-dirs", methods=["GET"])
def list_scan_dirs():
    cfg = ConfigManager.load()
    return jsonify({"scan_dirs": cfg.get("scan_dirs", [])})


@bp.route("/api/scan-dirs", methods=["POST"])
def add_scan_dir():
    data = request.json
    if not data or not data.get("path"):
        return jsonify({"error": "路径不能为空"}), 400
    path = data["path"].strip()
    cfg = ConfigManager.load()
    scan_dirs = cfg.get("scan_dirs", [])
    if path in scan_dirs:
        return jsonify({"error": "路径已存在"}), 400
    scan_dirs.append(path)
    cfg["scan_dirs"] = scan_dirs
    ConfigManager.save(cfg)
    return jsonify({"scan_dirs": scan_dirs})


@bp.route("/api/scan-dirs", methods=["DELETE"])
def remove_scan_dir():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "路径不能为空"}), 400
    cfg = ConfigManager.load()
    scan_dirs = cfg.get("scan_dirs", [])
    scan_dirs = [d for d in scan_dirs if d != path]
    cfg["scan_dirs"] = scan_dirs
    ConfigManager.save(cfg)
    return jsonify({"scan_dirs": scan_dirs})


@bp.route("/api/ui-settings", methods=["GET"])
def get_ui_settings():
    cfg = ConfigManager.load()
    defaults = ConfigManager._default_config()["ui_settings"]
    settings = {**defaults, **cfg.get("ui_settings", {})}
    return jsonify(settings)


@bp.route("/api/ui-settings", methods=["POST"])
def save_ui_settings():
    data = request.json or {}
    cfg = ConfigManager.load()
    current = cfg.get("ui_settings", {})
    current.update(data)
    cfg["ui_settings"] = current
    ConfigManager.save(cfg)
    return jsonify({"ok": True, "ui_settings": current})
