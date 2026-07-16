"""lark-cli 子进程底座(I4):argv 无 shell、显式 --profile、超时+进程组 SIGTERM、
并发排空 stdout/stderr(communicate 内部双线程)。REST 契约:以解析出预期字段为准,缺=UNKNOWN。"""
import json
import os
import signal
import subprocess

from . import constants


class RunResult:
    __slots__ = ("rc", "stdout", "stderr", "timed_out", "exc")

    def __init__(self, rc=0, stdout="", stderr="", timed_out=False, exc=None):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.exc = exc


class LarkRunner:
    """一次性 REST 调用。event consume 常驻消费不走这里(见 daemon_core.ConsumerManager)。"""

    def __init__(self, profile, lark_bin="lark-cli"):
        if not profile:
            raise ValueError("profile required (指纹钉死)")
        self.profile = profile
        self.lark_bin = lark_bin

    def run(self, args, timeout_s=constants.SEND_TIMEOUT_S, cwd=None):
        argv = [self.lark_bin] + [str(a) for a in args] + ["--profile", self.profile]
        try:
            p = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True)
        except OSError as e:
            return RunResult(rc=-1, exc=e)
        try:
            out, err = p.communicate(timeout=timeout_s)
            return RunResult(rc=p.returncode, stdout=out or "", stderr=err or "")
        except subprocess.TimeoutExpired:
            _kill_group(p, sig=signal.SIGTERM)
            try:
                out, err = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # REST 一次性调用无服务端订阅,末路 SIGKILL 防僵死(consume 进程绝不 -9)
                _kill_group(p, sig=signal.SIGKILL)
                try:
                    out, err = p.communicate(timeout=5)
                except Exception:
                    out, err = "", ""
            return RunResult(rc=p.returncode if p.returncode is not None else -1,
                             stdout=out or "", stderr=err or "", timed_out=True)


def _kill_group(p, sig):
    try:
        os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.send_signal(sig)
        except Exception:
            pass


def parse_envelope(stdout):
    """解析 lark-cli JSON 信封;容忍 `_notice` 附加键与前后噪声行。失败 → None。"""
    if not stdout:
        return None
    s = stdout.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    for line in s.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def envelope_ok(env):
    return bool(env) and env.get("ok") is True


def data_of(env):
    d = (env or {}).get("data")
    return d if isinstance(d, dict) else {}
