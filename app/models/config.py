import os
import yaml

# 项目根目录: app/models/config.py -> app/ -> 项目根
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yml")

DEFAULT_EXTENSIONS = [".nsp", ".nsz", ".xci", ".xcz"]


class ConfigManager:
    """加载和保存 config.yml"""

    @staticmethod
    def load():
        if not os.path.exists(CONFIG_PATH):
            return ConfigManager._default_config()
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return ConfigManager._merge_defaults(cfg)

    @staticmethod
    def save(cfg):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _default_config():
        return {
            "server": {"host": "0.0.0.0", "port": 8090},
            "scan_dirs": [],
            "scan_extensions": DEFAULT_EXTENSIONS,
            "scan_interval": 300,
            "ftp_servers": [],
            "ui_settings": {"page_size": 10, "show_all_files": False, "language": "zh"},
        }

    @staticmethod
    def _merge_defaults(cfg):
        defaults = ConfigManager._default_config()
        for key in defaults:
            if key not in cfg:
                cfg[key] = defaults[key]
        return cfg


def mask_server(s):
    """返回不包含明文密码的服务器信息"""
    s_copy = dict(s)
    if s_copy.get("password"):
        s_copy["password_masked"] = "•" * len(s_copy["password"])
        s_copy["has_password"] = True
    else:
        s_copy["has_password"] = False
    del s_copy["password"]
    return s_copy
