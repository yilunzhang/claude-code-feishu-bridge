"""plugin 版本 / 代码身份(plugin 化)。
- plugin_version:读 .claude-plugin/plugin.json 的 version。
- code_identity:pkg_root 绝对路径 + plugin version + 可选 git build。
  用途:①心跳记录哪个 install/版本跑的 hook(MAJOR 2 防假阳性);
        ②daemon 启动时发布,CLI bind 前比对,检测「更新/迁移后复用跑旧代码的旧 daemon」(MAJOR 3)。
"""
import json
import subprocess

from . import paths


def plugin_version():
    try:
        pj = json.loads((paths.pkg_root() / ".claude-plugin" / "plugin.json").read_text())
        v = pj.get("version")
        return str(v) if v else "unknown"
    except Exception:
        return "unknown"


def git_build():
    """best-effort:git 短 HEAD;非 git 检出(如 marketplace 安装)或失败 → None。"""
    try:
        r = subprocess.run(
            ["git", "-C", str(paths.pkg_root()), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def install_identity():
    """心跳用:只 pkg_root + plugin_version(hook 要快,不跑 git)。(str, str)。"""
    return (str(paths.pkg_root()), plugin_version())


def code_identity_str():
    """daemon 版本检测用:pkg_root|plugin_version|git(git 可空)。"""
    root, ver = install_identity()
    return f"{root}|{ver}|{git_build() or ''}"
