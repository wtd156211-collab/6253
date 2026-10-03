"""命令行入口：python -m checktree build|check。"""

from __future__ import annotations

import argparse
import sys

from .core import InputError
from .engine import CheckFailure, build_tree, check_tree

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_FAILED = 2


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(EXIT_INPUT, f"参数错误: {message}\n")


def _make_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m checktree")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="从分块清单建树")
    build.add_argument("--blocks", required=True)
    build.add_argument("--tree", required=True)
    build.add_argument("--branch", type=int, default=4)

    check = sub.add_parser("check", help="对照历史树增量核对")
    check.add_argument("--tree", required=True)
    check.add_argument("--blocks", required=True)
    check.add_argument("--changes", required=True)
    check.add_argument("--out")
    check.add_argument("--page")
    return parser


def main(argv=None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_tree(args.blocks, args.tree, args.branch)
            for line in result.stdout_lines():
                print(line)
            return EXIT_OK
        try:
            result = check_tree(
                args.tree, args.blocks, args.changes,
                out_path=args.out, page_path=args.page,
            )
        except CheckFailure as failure:
            print(failure.stdout_line())
            return EXIT_FAILED
        for line in result.stdout_lines():
            print(line)
        return EXIT_OK
    except InputError as exc:
        print(f"输入不可用: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except OSError as exc:
        print(f"输入不可用: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
