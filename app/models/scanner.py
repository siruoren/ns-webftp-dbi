import os
import time
import threading
from datetime import datetime

from app.models.config import ConfigManager, DEFAULT_EXTENSIONS


class FileScanner:
    """扫描目录下所有 Switch 安装包"""

    _SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".gitkeep"}

    @staticmethod
    def scan(scan_dirs, extensions=None, show_all=False):
        if extensions is None:
            extensions = DEFAULT_EXTENSIONS
        ext_set = {e.lower() for e in extensions}
        results = []
        dir_mtime_cache = {}
        for scan_dir in scan_dirs:
            scan_dir = os.path.expanduser(scan_dir)
            if not os.path.isdir(scan_dir):
                continue
            for root, dirs, files in os.walk(scan_dir):
                if root not in dir_mtime_cache:
                    try:
                        dir_mtime_cache[root] = os.path.getmtime(root)
                    except OSError:
                        dir_mtime_cache[root] = 0
                dir_mtime = dir_mtime_cache[root]
                for fname in files:
                    if fname.startswith(".") or fname.lower() in FileScanner._SKIP_NAMES:
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if not show_all and ext not in ext_set:
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                        mtime = os.path.getmtime(fpath)
                    except OSError:
                        continue
                    results.append({
                        "name": fname,
                        "path": fpath,
                        "dir": root,
                        "dir_mtime": dir_mtime,
                        "dir_mtime_str": datetime.fromtimestamp(dir_mtime).strftime("%Y-%m-%d %H:%M") if dir_mtime else "",
                        "size": fsize,
                        "mtime": mtime,
                        "mtime_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                        "ext": ext,
                    })
        results.sort(key=lambda x: (x["dir_mtime"], x["mtime"]), reverse=True)
        return results


class FileScanManager:
    """管理文件扫描缓存，定时后台刷新，同一时间只允许一个扫描任务"""

    _instance = None
    _init_lock = threading.Lock()

    def __init__(self):
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._cached_files = []
        self._cached_all_files = []
        self._last_scan_time = 0
        self._stop_event = threading.Event()
        self._thread = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def start_auto_scan(self, interval=60):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._auto_loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop_auto_scan(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _auto_loop(self, interval):
        while not self._stop_event.is_set():
            try:
                self.do_scan()
                print(f"扫描完成: {len(self._cached_files)} 个安装包")
            except Exception as e:
                print(f"[FileScanManager] auto scan error: {e}")
            self._stop_event.wait(interval)

    def do_scan(self):
        if not self._scan_lock.acquire(blocking=False):
            return False
        try:
            self._scanning = True
            cfg = ConfigManager.load()
            scan_dirs = cfg.get("scan_dirs", [])
            exts = cfg.get("scan_extensions", DEFAULT_EXTENSIONS)
            all_results = FileScanner.scan(scan_dirs, exts, show_all=True)
            ext_set = {e.lower() for e in exts}
            self._cached_all_files = all_results
            self._cached_files = [
                f for f in all_results
                if os.path.splitext(f["name"])[1].lower() in ext_set
            ]
            self._last_scan_time = time.time()
            return True
        finally:
            self._scanning = False
            self._scan_lock.release()

    @property
    def is_scanning(self):
        return self._scanning

    @property
    def last_scan_time(self):
        return self._last_scan_time

    def get_files(self, show_all=False):
        return self._cached_all_files if show_all else self._cached_files
