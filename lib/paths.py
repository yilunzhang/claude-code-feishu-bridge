"""路径解析。测试经 FEISHU_BRIDGE_DATA_DIR / FEISHU_BRIDGE_SETTINGS_PATH 重定向,
绝不在 import 时创建真实目录。"""
import os
import pathlib


def skill_root():
    return pathlib.Path(__file__).resolve().parents[1]


def data_dir():
    env = os.environ.get("FEISHU_BRIDGE_DATA_DIR")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".claude" / "data" / "feishu-bridge"


def ensure_data_dir():
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    (d / "media").mkdir(exist_ok=True)
    return d


def db_path():
    return data_dir() / "bridge.db"


def schema_path():
    return skill_root() / "schema.sql"


def config_path():
    return data_dir() / "config.json"


def lock_path():
    return data_dir() / "bridge.lock"


def bootstrap_lock_path():
    return data_dir() / "bootstrap.lock"


def ensure_lock_path():
    return data_dir() / "ensure.lock"  # ensure-daemon 接管 singleflight(r2-M1④)


def daemon_log_path():
    return data_dir() / "daemon.log"


def hook_drops_path():
    return data_dir() / "hook_drops.log"


def media_root():
    return data_dir() / "media"


def settings_json_path():
    env = os.environ.get("FEISHU_BRIDGE_SETTINGS_PATH")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".claude" / "settings.json"
