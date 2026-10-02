"""命令行入口：python -m checktree build|check（README 第 1、4.4 节）。"""
from __future__ import annotations

import argparse
import sys

from .engine import CheckFailed, run_build, run_check
from .treefile import InputError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"参数错误: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m checktree")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="给分块清单建树")
    build.add_argument("--blocks", required=True, help="分块清单 .tsv")
    build.add_argument("--tree", required=True, help="写出的树文件")
    build.add_argument("--branch", type=int, default=4, help="分支因子 k（缺省 4）")

    check = sub.add_parser("check", help="拿新清单与历史树核对")
    check.add_argument("--tree", required=True, help="上一轮树文件")
    check.add_argument("--blocks", required=True, help="新分块清单 .tsv")
    check.add_argument("--changes", required=True, help="变更集 .tsv")
    check.add_argument("--out", help="写回的新树文件")
    check.add_argument("--page", help="写出的差异页面 .html")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            sys.stdout.write(run_build(args.blocks, args.tree, args.branch))
            return 0
        text, code = run_check(
            args.tree, args.blocks, args.changes, out_path=args.out, page_path=args.page
        )
        sys.stdout.write(text)
        return code
    except CheckFailed as failed:
        sys.stdout.write(f"核对 不通过 {failed.reason}\n")
        return 2
    except InputError as exc:
        sys.stderr.write(f"输入不可用: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"输入不可用: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
