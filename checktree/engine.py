"""build 与 check 的引擎：只顺变化路径重算，内存与块数无关。"""

from __future__ import annotations

import os

from .core import (
    ACTION_APPEND,
    BLOCKLIST_RECORD_SIZE,
    ACTION_DELETE,
    ACTION_REPLACE,
    Change,
    InputError,
    RECORD_SIZE,
    TreeHeader,
    blocklist_count,
    build_header,
    empty_root,
    format_record,
    iter_blocklist,
    layer_counts,
    leaf_hash,
    parent_hash,
    prefix_offsets,
    parse_blocklist_record,
    read_changes,
    read_tree_header,
    tree_depth,
)


class BuildResult:
    def __init__(self, blocks: int, layers: int, root: bytes):
        self.blocks = blocks
        self.layers = layers
        self.root = root

    def stdout_lines(self) -> list[str]:
        return [
            f"块数 {self.blocks}",
            f"层数 {self.layers}",
            f"根 {self.root.hex()}",
        ]


def build_tree(blocklist_path: str, tree_path: str, branch: int) -> BuildResult:
    if not (2 <= branch <= 64):
        raise InputError("分支因子必须在 2..64")
    blocks = blocklist_count(blocklist_path)
    depth = tree_depth(blocks, branch)
    counts = layer_counts(blocks, branch, depth)
    prefixes = prefix_offsets(counts)
    header = build_header(branch, blocks, [])
    header_size = len(header)

    root = empty_root()
    pending: list[list[bytes]] = [[] for _ in range(depth + 1)]
    with open(tree_path, "wb") as out:
        out.write(header)

        def emit(level: int, index: int, value: bytes) -> None:
            nonlocal root
            out.seek(header_size + RECORD_SIZE * (prefixes[level] + index))
            out.write(format_record(level, index, value))
            if level == depth:
                root = value
                return
            pending[level].append(value)
            if len(pending[level]) == branch or index == counts[level] - 1:
                parent = parent_hash(pending[level])
                pending[level] = []
                emit(level + 1, index // branch, parent)

        for index, digest in iter_blocklist(blocklist_path):
            emit(0, index, leaf_hash(index, digest))

    return BuildResult(blocks, depth + 1 if blocks else 0, root)


REASON_POSITION = "位置非法"
REASON_FROZEN = "已冻结"
REASON_STALE = "旧摘要与树不符"
REASON_BLOCKLIST = "清单与变更集不符"
REASON_ORDER = (REASON_POSITION, REASON_FROZEN, REASON_STALE, REASON_BLOCKLIST)


class CheckFailure(Exception):
    def __init__(self, block, reason):
        super().__init__(reason)
        self.block = block
        self.reason = reason

    def stdout_line(self) -> str:
        if self.block is None:
            return f"核对 不通过 {self.reason}"
        return f"核对 不通过 块 {self.block} {self.reason}"


class CheckResult:
    def __init__(self, blocks, changed_blocks, recomputed, root, paths):
        self.blocks = blocks
        self.changed_blocks = changed_blocks
        self.recomputed = recomputed
        self.root = root
        self.paths = paths

    def stdout_lines(self) -> list[str]:
        changed = (
            " ".join(str(item) for item in self.changed_blocks)
            if self.changed_blocks
            else "-"
        )
        recomputed = (
            " ".join(f"{d}:{i}" for d, i in self.recomputed)
            if self.recomputed
            else "-"
        )
        lines = [
            "核对 通过",
            f"块数 {self.blocks}",
            f"变化块 {changed}",
            f"重算节点 {recomputed}",
            f"重算数 {len(self.recomputed)}",
            f"根 {self.root.hex()}",
        ]
        for block in self.changed_blocks:
            if block in self.paths:
                lines.append(f"路径 {block} {self.paths[block]}")
        return lines


def _read_node(handle, header: TreeHeader, depth: int, index: int) -> bytes:
    handle.seek(header.record_offset(depth, index))
    raw = handle.read(RECORD_SIZE)
    if len(raw) != RECORD_SIZE:
        raise InputError("树节点区读取越界")
    return bytes.fromhex(raw[12:76].decode("ascii"))


def _validate_changes(header: TreeHeader, changes: list[Change],
                      new_blocks: int, blocklist_handle,
                      tree_handle) -> None:
    old_blocks = header.blocks
    frozen = set(header.frozen)
    appends = [c for c in changes if c.action == ACTION_APPEND]
    deletes = [c for c in changes if c.action == ACTION_DELETE]
    implied = old_blocks + len(appends) - len(deletes)
    if implied != new_blocks:
        raise CheckFailure(None, "清单块数与变更集不符")

    violations: dict[int, set[str]] = {}

    def flag(block: int, reason: str) -> None:
        violations.setdefault(block, set()).add(reason)

    previous = -1
    for change in changes:
        if change.block <= previous:
            flag(change.block, REASON_POSITION)
        previous = max(previous, change.block)

    if appends and deletes:
        for change in appends + deletes:
            flag(change.block, REASON_POSITION)
    for rank, change in enumerate(appends):
        if change.block != old_blocks + rank:
            flag(change.block, REASON_POSITION)
    delete_base = old_blocks - len(deletes)
    for rank, change in enumerate(deletes):
        if change.block != delete_base + rank:
            flag(change.block, REASON_POSITION)
    for change in changes:
        if change.action == ACTION_REPLACE:
            limit = delete_base if deletes else old_blocks
            if change.block >= limit:
                flag(change.block, REASON_POSITION)

    for change in changes:
        if change.block in frozen:
            flag(change.block, REASON_FROZEN)

    for change in changes:
        if REASON_POSITION in violations.get(change.block, ()):
            continue
        if change.action in (ACTION_REPLACE, ACTION_DELETE):
            stored = _read_node(tree_handle, header, 0, change.block)
            if leaf_hash(change.block, change.old_digest) != stored:
                flag(change.block, REASON_STALE)
        if change.action in (ACTION_REPLACE, ACTION_APPEND):
            blocklist_handle.seek(change.block * BLOCKLIST_RECORD_SIZE)
            listed = parse_blocklist_record(
                blocklist_handle.read(BLOCKLIST_RECORD_SIZE), change.block)
            if listed != change.new_digest:
                flag(change.block, REASON_BLOCKLIST)

    if violations:
        block = min(violations)
        for reason in REASON_ORDER:
            if reason in violations[block]:
                raise CheckFailure(block, reason)
        raise AssertionError("unreachable")


def check_tree(tree_path: str, blocklist_path: str, changes_path: str,
               out_path: str | None = None,
               page_path: str | None = None) -> CheckResult:
    header = read_tree_header(tree_path)
    changes = read_changes(changes_path)
    new_blocks = blocklist_count(blocklist_path)
    branch = header.branch

    with open(tree_path, "rb") as tree_handle, \
            open(blocklist_path, "rb") as blocklist_handle:
        _validate_changes(header, changes, new_blocks, blocklist_handle,
                          tree_handle)

        new_depth = tree_depth(new_blocks, branch)
        new_counts = layer_counts(new_blocks, branch, new_depth)

        new_values: dict[tuple[int, int], bytes] = {}
        changed_blocks = [c.block for c in changes]
        live_changed = [c.block for c in changes
                        if c.action != ACTION_DELETE]

        for change in changes:
            if change.action != ACTION_DELETE:
                new_values[(0, change.block)] = leaf_hash(
                    change.block, change.new_digest)

        recomputed: set[tuple[int, int]] = set(new_values)
        powers = [branch**d for d in range(new_depth + 1)]
        for block in changed_blocks:
            for level in range(1, new_depth + 1):
                index = block // powers[level]
                if index < new_counts[level]:
                    recomputed.add((level, index))

        old_counts = header.counts
        for level in range(1, new_depth + 1):
            level_nodes = sorted(i for d, i in recomputed if d == level)
            old_below = old_counts[level - 1] if level - 1 <= header.depth else 0
            cursor = 0
            while cursor < len(level_nodes):
                # 连续父节点的子区间也连续，合并成一次读
                run_end = cursor + 1
                while (run_end < len(level_nodes)
                       and level_nodes[run_end]
                       == level_nodes[run_end - 1] + 1):
                    run_end += 1
                first_child = level_nodes[cursor] * branch
                last_child = min(level_nodes[run_end - 1] * branch + branch,
                                 new_counts[level - 1])
                read_upto = min(last_child, old_below)
                chunk = b""
                if read_upto > first_child:
                    tree_handle.seek(
                        header.record_offset(level - 1, first_child))
                    chunk = tree_handle.read(
                        RECORD_SIZE * (read_upto - first_child))
                    if len(chunk) != RECORD_SIZE * (read_upto - first_child):
                        raise InputError("树节点区读取越界")
                for index in level_nodes[cursor:run_end]:
                    first = index * branch
                    last = min(first + branch, new_counts[level - 1])
                    children = []
                    for child in range(first, last):
                        value = new_values.get((level - 1, child))
                        if value is None:
                            if child >= old_below:
                                raise AssertionError("新位置必然在重算集合内")
                            off = (child - first_child) * RECORD_SIZE
                            value = bytes.fromhex(
                                chunk[off + 12:off + 76].decode("ascii"))
                        children.append(value)
                    new_values[(level, index)] = parent_hash(children)
                cursor = run_end

        if new_blocks == 0:
            root = empty_root()
        elif (new_depth, 0) in new_values:
            root = new_values[(new_depth, 0)]
        else:
            root = _read_node(tree_handle, header, new_depth, 0)

        ordered = sorted(recomputed)
        paths = {}
        for block in live_changed:
            if new_depth == 0:
                paths[block] = "-"
            else:
                indices = [
                    str((block // powers[level]) % branch)
                    for level in range(new_depth - 1, -1, -1)
                ]
                paths[block] = "/".join(indices)

        result = CheckResult(new_blocks, changed_blocks, ordered, root, paths)

        if out_path is not None:
            _write_tree(tree_handle, header, out_path, new_blocks,
                        new_depth, new_counts, new_values)

    if page_path is not None:
        from .page import write_page
        with open(page_path, "w", encoding="utf-8", newline="") as handle:
            write_page(result, branch, new_depth, handle)

    return result


def _copy_records(tree_handle, out, header: TreeHeader, level: int,
                  start: int, stop: int) -> None:
    """把旧树第 level 层 [start, stop) 的记录原样批量拷到 out。"""
    if stop <= start:
        return
    tree_handle.seek(header.record_offset(level, start))
    remaining = (stop - start) * RECORD_SIZE
    while remaining > 0:
        chunk = tree_handle.read(min(remaining, 1 << 20))
        if not chunk:
            raise InputError("树节点区读取越界")
        out.write(chunk)
        remaining -= len(chunk)


def _write_tree(tree_handle, header: TreeHeader, out_path: str,
                new_blocks: int, new_depth: int, new_counts: list[int],
                new_values: dict[tuple[int, int], bytes]) -> None:
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "wb") as out:
        out.write(header.header_bytes_for(new_blocks))
        for level in range(new_depth + 1):
            old_count = header.counts[level] if level <= header.depth else 0
            count = new_counts[level]
            changed_here = sorted(
                i for d, i in new_values if d == level)
            cursor = 0
            for index in changed_here:
                if index > cursor:
                    _copy_records(tree_handle, out, header, level,
                                  cursor, min(index, old_count))
                out.write(format_record(level, index, new_values[(level, index)]))
                cursor = index + 1
            if cursor < count:
                if count > old_count:
                    raise AssertionError("新位置必然在重算集合内")
                _copy_records(tree_handle, out, header, level, cursor, count)
    os.replace(tmp_path, out_path)
