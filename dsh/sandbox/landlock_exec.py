"""
dsh.sandbox.landlock_exec —— Landlock 包装器进程（``python -m``）。

argv: ``<workspace> <命令 argv...>``——先应用 Landlock（只读 FS + 工作区
可写），再 exec 目标命令。应用失败（内核不支持等）打印诊断后以 127 退出
（如实失败，绝不静默放行）。
"""
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("landlock_exec: usage: <workspace> <command...>",
              file=sys.stderr)
        return 127
    workspace = sys.argv[1]
    command = sys.argv[2:]
    try:
        from dsh.sandbox.landlock import apply_workspace_landlock
        apply_workspace_landlock(workspace)
    except OSError as exc:
        print(f"landlock_exec: {exc}", file=sys.stderr)
        return 127
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"landlock_exec: cannot exec {command[0]!r}: {exc}",
              file=sys.stderr)
        return 127
    return 127  # execvp 成功则不会返回


if __name__ == "__main__":
    sys.exit(main())
