#!/usr/bin/env python3
"""CC Stop hook 入口:stdin JSON → hooklib.stop_hook_entry。
永远 exit 0;一切异常在 hooklib 内 fail-closed。唯一会让 CC 继续本轮的是语言提醒
(stdout 输出 block 决定,每轮至多一次)。"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lib import hooklib  # noqa: E402


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    res = hooklib.stop_hook_entry(payload, start_pid=os.getppid())
    try:
        out = hooklib.stop_hook_stdout(res)
        if out:
            print(out)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
