"""build / check 引擎（README 第 3、4 节）。

核对只顺着变化块的路径走：变化块的叶与它们在变更后仍存在的祖先，
按坐标去重后重算，其余记录一个字节都不动。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field

from .core import (
    BLOCKLIST_DIGEST_LENGTH,
    BLOCKLIST_DIGEST_OFFSET,
    BLOCKLIST_RECORD,
    EMPTY_ROOT,
    TREE_RECORD,
    block_path,
    format_record,
    layer_counts,
    leaf_hash,
    node_hash,
)
from .treefile import InputError, TreeFile

ACTION_REPLACE = "替换"
ACTION_APPEND = "新增"
ACTION_DELETE = "删除"
_ACTIONS = (ACTION_REPLACE, ACTION_APPEND, ACTION_DELETE)


class CheckFailed(Exception):
    """核对不通过；reason 是不带前缀的原因文本。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Change:
    block: int
    action: str
    old: str | None
    new: str | None


@dataclass
class CheckResult:
    blocks: int
    changed_blocks: list[int]
    deleted_blocks: list[int]
    recomputed: list[tuple[int, int]]
    root: str
    paths: dict[int, str]
    branch: int
    depth: int
    new_hashes: dict = field(default_factory=dict)

    @property
    def changed_text(self) -> str:
        return " ".join(str(b) for b in self.changed_blocks) or "-"

    @property
    def recomputed_text(self) -> str:
        return " ".join(f"{d}:{i}" for d, i in self.recomputed) or "-"


def _parse_digest(text: str, where: str) -> str | None:
    if text == "-":
        return None
    if len(text) != 64 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise InputError(f"{where} 摘要不是 64 位十六进制")
    return text.lower()


def parse_changes(path: str) -> list[Change]:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            raw_lines = fh.read().splitlines()
    except FileNotFoundError:
        raise InputError(f"变更集不存在: {path}") from None
    changes = []
    for lineno, line in enumerate(raw_lines, 1):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise InputError(f"变更集第 {lineno} 行不是 4 列")
        block_text, action, old_text, new_text = parts
        if not block_text.isdigit():
            raise InputError(f"变更集第 {lineno} 行块号无法解析")
        if action not in _ACTIONS:
            raise InputError(f"变更集第 {lineno} 行动作未知: {action}")
        old = _parse_digest(old_text, f"变更集第 {lineno} 行旧")
        new = _parse_digest(new_text, f"变更集第 {lineno} 行新")
        if action == ACTION_APPEND and old is not None:
            raise InputError(f"变更集第 {lineno} 行新增的旧摘要应为 -")
        if action == ACTION_DELETE and new is not None:
            raise InputError(f"变更集第 {lineno} 行删除的新摘要应为 -")
        if action == ACTION_REPLACE and (old is None or new is None):
            raise InputError(f"变更集第 {lineno} 行替换需要新旧摘要")
        changes.append(Change(int(block_text), action, old, new))
    return changes


def blocklist_count(path: str) -> int:
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        raise InputError(f"分块清单不存在: {path}") from None
    if size % BLOCKLIST_RECORD:
        raise InputError(f"分块清单长度不是 {BLOCKLIST_RECORD} 的整数倍: {path}")
    return size // BLOCKLIST_RECORD


def read_blocklist_digest(fh, block: int) -> str:
    fh.seek(block * BLOCKLIST_RECORD + BLOCKLIST_DIGEST_OFFSET)
    data = fh.read(BLOCKLIST_DIGEST_LENGTH)
    if len(data) != BLOCKLIST_DIGEST_LENGTH:
        raise InputError(f"分块清单记录缺失: 块 {block}")
    return data.decode("ascii")


def _validate_positions(changes: list[Change], old_blocks: int) -> None:
    """位置合法性：升序不重复、替换落在旧范围内、增删只在末尾且不同时出现。"""
    bad = None

    def mark(block: int) -> None:
        nonlocal bad
        if bad is None or block < bad:
            bad = block

    prev = -1
    for change in changes:
        if change.block <= prev:
            mark(change.block)
        prev = change.block
    appends = [c for c in changes if c.action == ACTION_APPEND]
    deletes = [c for c in changes if c.action == ACTION_DELETE]
    if appends and deletes:
        mark(appends[0].block)
    for seq, change in enumerate(appends):
        if change.block != old_blocks + seq:
            mark(change.block)
    for seq, change in enumerate(deletes):
        if change.block != old_blocks - len(deletes) + seq:
            mark(change.block)
    for change in changes:
        if change.action == ACTION_REPLACE and change.block >= old_blocks:
            mark(change.block)
    if bad is not None:
        raise CheckFailed(f"块 {bad} 位置非法")


def _validate_entries(tf: TreeFile, blocks_path: str, changes: list[Change]) -> None:
    """逐条核对：已冻结、旧摘要与树不符、清单与变更集不符；报最小块号。"""
    frozen = set(tf.frozen)
    violations: list[tuple[int, int, str]] = []
    with open(blocks_path, "rb") as blocks_fh:
        for change in changes:
            block = change.block
            if block in frozen:
                violations.append((block, 0, f"块 {block} 已冻结"))
                continue
            if change.action in (ACTION_REPLACE, ACTION_DELETE):
                expect = leaf_hash(block, bytes.fromhex(change.old))
                if tf.read_node(0, block) != expect:
                    violations.append((block, 1, f"块 {block} 旧摘要与树不符"))
            if change.action in (ACTION_REPLACE, ACTION_APPEND):
                digest = read_blocklist_digest(blocks_fh, block)
                if digest.lower() != change.new:
                    violations.append((block, 2, f"块 {block} 清单与变更集不符"))
    if violations:
        violations.sort(key=lambda item: (item[0], item[1]))
        raise CheckFailed(violations[0][2])


def _recompute(tf: TreeFile, changes: list[Change], new_blocks: int) -> CheckResult:
    """只重算变化块的叶与它们在变更后仍存在的祖先（按坐标去重）。"""
    branch = tf.branch
    new_counts = layer_counts(new_blocks, branch)
    depth = len(new_counts) - 1
    leaf_changes = {c.block: c.new for c in changes if c.action != ACTION_DELETE}
    deleted = sorted(c.block for c in changes if c.action == ACTION_DELETE)

    recomputed: set[tuple[int, int]] = {(0, b) for b in leaf_changes}
    for change in changes:
        index = change.block
        for layer in range(1, len(new_counts)):
            index //= branch
            if index < new_counts[layer]:
                recomputed.add((layer, index))

    new_hashes: dict[tuple[int, int], bytes] = {}
    for block, digest_hex in leaf_changes.items():
        new_hashes[(0, block)] = leaf_hash(block, bytes.fromhex(digest_hex))
    for layer in range(1, len(new_counts)):
        for index in sorted(i for d, i in recomputed if d == layer):
            start = index * branch
            end = min(start + branch, new_counts[layer - 1])
            children = []
            for child in range(start, end):
                child_hash = new_hashes.get((layer - 1, child))
                if child_hash is None:
                    child_hash = tf.read_node(layer - 1, child)
                children.append(child_hash)
            new_hashes[(layer, index)] = node_hash(children)

    if not new_counts:
        root = EMPTY_ROOT
    else:
        top = new_hashes.get((depth, 0))
        if top is None:
            top = tf.read_node(depth, 0)
        root = top.hex()

    changed_blocks = sorted(c.block for c in changes)
    paths = {
        block: block_path(block, new_counts, branch)
        for block in changed_blocks
        if block not in set(deleted)
    }
    return CheckResult(
        blocks=new_blocks,
        changed_blocks=changed_blocks,
        deleted_blocks=deleted,
        recomputed=sorted(recomputed),
        root=root,
        paths=paths,
        branch=branch,
        depth=max(depth, 0),
        new_hashes=new_hashes,
    )


def _format_success(result: CheckResult) -> str:
    lines = [
        "核对 通过",
        f"块数 {result.blocks}",
        f"变化块 {result.changed_text}",
        f"重算节点 {result.recomputed_text}",
        f"重算数 {len(result.recomputed)}",
        f"根 {result.root}",
    ]
    for block in sorted(result.paths):
        lines.append(f"路径 {block} {result.paths[block]}")
    return "\n".join(lines) + "\n"


def _tree_header(branch: int, blocks: int, frozen_text: str) -> bytes:
    return (
        "format 1\n"
        f"branch {branch}\n"
        f"blocks {blocks}\n"
        f"frozen {frozen_text}\n"
        "nodes\n"
    ).encode("utf-8")


def write_tree(tf: TreeFile, out_path: str, result: CheckResult) -> None:
    """把这一轮的结果写回树；没涉及的记录一个字节都不改。

    层布局不变时只就地改写重算的记录；布局变化（末尾增删）时流式合并，
    未变化的记录段从旧树原字节拷贝。
    """
    new_counts = layer_counts(result.blocks, tf.branch)
    new_hashes = result.new_hashes
    if new_counts == tf.counts:
        if os.path.abspath(out_path) != os.path.abspath(tf.path):
            shutil.copyfile(tf.path, out_path)
        with open(out_path, "r+b") as fh:
            for (layer, index), hash_bytes in new_hashes.items():
                fh.seek(tf.record_offset(layer, index))
                fh.write(format_record(layer, index, hash_bytes))
        return

    header = _tree_header(tf.branch, result.blocks, tf.frozen_text)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".checktree-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out_fh, open(tf.path, "rb") as in_fh:
            out_fh.write(header)
            for layer, count in enumerate(new_counts):
                index = 0
                while index < count:
                    hash_bytes = new_hashes.get((layer, index))
                    if hash_bytes is not None:
                        out_fh.write(format_record(layer, index, hash_bytes))
                        index += 1
                        continue
                    run_end = index
                    while run_end < count and (layer, run_end) not in new_hashes:
                        run_end += 1
                    if run_end > tf.counts[layer]:
                        raise InputError(f"树文件记录缺失: 层 {layer} 序号 {index}")
                    in_fh.seek(tf.record_offset(layer, index))
                    remaining = TREE_RECORD * (run_end - index)
                    while remaining:
                        chunk = in_fh.read(min(1 << 20, remaining))
                        if not chunk:
                            raise InputError(f"树文件记录缺失: 层 {layer} 序号 {index}")
                        out_fh.write(chunk)
                        remaining -= len(chunk)
                    index = run_end
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def run_check(
    tree_path: str,
    blocks_path: str,
    changes_path: str,
    out_path: str | None = None,
    page_path: str | None = None,
) -> tuple[str, int]:
    """一轮核对；通过时返回 (stdout, 0)，不通过抛 CheckFailed。"""
    tf = TreeFile.open(tree_path)
    try:
        changes = parse_changes(changes_path)
        new_blocks = blocklist_count(blocks_path)
        _validate_positions(changes, tf.blocks)
        appends = sum(1 for c in changes if c.action == ACTION_APPEND)
        deletes = sum(1 for c in changes if c.action == ACTION_DELETE)
        if tf.blocks + appends - deletes != new_blocks:
            raise CheckFailed("清单块数与变更集不符")
        _validate_entries(tf, blocks_path, changes)
        result = _recompute(tf, changes, new_blocks)
        if out_path is not None:
            write_tree(tf, out_path, result)
        if page_path is not None:
            from .page import render_page

            with open(page_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_page(result))
        return _format_success(result), 0
    finally:
        tf.close()


def run_build(blocks_path: str, tree_path: str, branch: int) -> str:
    """全量建树；允许重算全部节点，内存只跟单块缓冲区走。"""
    if not 2 <= branch <= 64:
        raise InputError("branch 需在 2..64")
    blocks = blocklist_count(blocks_path)
    counts = layer_counts(blocks, branch)

    spills = [tempfile.TemporaryFile() for _ in counts]
    try:
        if counts:
            index = 0
            with open(blocks_path, "rb") as fh:
                while True:
                    chunk = fh.read(BLOCKLIST_RECORD * 4096)
                    if not chunk:
                        break
                    for off in range(0, len(chunk), BLOCKLIST_RECORD):
                        digest_hex = chunk[
                            off + BLOCKLIST_DIGEST_OFFSET : off + BLOCKLIST_DIGEST_OFFSET + 64
                        ].decode("ascii")
                        spills[0].write(leaf_hash(index, bytes.fromhex(digest_hex)))
                        index += 1
            for layer in range(len(counts) - 1):
                spills[layer].seek(0)
                while True:
                    buf = spills[layer].read(32 * branch)
                    if not buf:
                        break
                    children = [buf[o : o + 32] for o in range(0, len(buf), 32)]
                    spills[layer + 1].write(node_hash(children))

        header = _tree_header(branch, blocks, "-")
        with open(tree_path, "wb") as out_fh:
            out_fh.write(header)
            for layer, count in enumerate(counts):
                spills[layer].seek(0)
                index = 0
                while index < count:
                    buf = spills[layer].read(32 * 8192)
                    if not buf:
                        break
                    lines = []
                    for off in range(0, len(buf), 32):
                        lines.append(f"{layer:02d}:{index:08d} {buf[off:off + 32].hex()}\n")
                        index += 1
                    out_fh.write("".join(lines).encode("ascii"))

        if not counts:
            root = EMPTY_ROOT
        else:
            spills[-1].seek(0)
            root = spills[-1].read(32).hex()
        return f"块数 {blocks}\n层数 {len(counts)}\n根 {root}\n"
    finally:
        for spill in spills:
            spill.close()
