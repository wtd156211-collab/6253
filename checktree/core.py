"""口径常量、哈希公式、分层算术与输入文件解析。"""

from __future__ import annotations

import hashlib
import os

BLOCK_SIZE = 4096
RECORD_SIZE = 77
HEADER_LINES = 5
BLOCKLIST_RECORD_SIZE = 93

ACTION_REPLACE = "替换"
ACTION_APPEND = "新增"
ACTION_DELETE = "删除"
ACTIONS = (ACTION_REPLACE, ACTION_APPEND, ACTION_DELETE)

DASH = "-"


class InputError(Exception):
    """输入文件不可用（退出码 1）。"""


def leaf_hash(index: int, digest: bytes) -> bytes:
    if len(digest) != 32:
        raise ValueError("摘要必须是 32 字节")
    return hashlib.sha256(b"\x00" + index.to_bytes(8, "big") + digest).digest()


def parent_hash(children: list[bytes]) -> bytes:
    payload = b"".join(children)
    return hashlib.sha256(
        b"\x01" + len(children).to_bytes(8, "big") + payload
    ).digest()


def empty_root() -> bytes:
    return hashlib.sha256(b"").digest()


def layer_count(blocks: int, branch: int, depth: int) -> int:
    """第 depth 层的节点数：ceil(blocks / branch**depth)。"""
    return -(-blocks // branch**depth)


def tree_depth(blocks: int, branch: int) -> int:
    """根所在层号；空清单约定为 0（树里没有任何记录）。"""
    if blocks <= 1:
        return 0
    depth = 0
    while blocks > branch**depth:
        depth += 1
    return depth


def layer_counts(blocks: int, branch: int, depth: int) -> list[int]:
    return [layer_count(blocks, branch, d) for d in range(depth + 1)]


def prefix_offsets(counts: list[int]) -> list[int]:
    """前 d 层节点总数。"""
    prefixes = [0]
    total = 0
    for count in counts[:-1]:
        total += count
        prefixes.append(total)
    return prefixes


def format_record(depth: int, index: int, value: bytes) -> bytes:
    return f"{depth:02d}:{index:08d} {value.hex()}\n".encode("ascii")


def parse_record(raw: bytes) -> tuple[int, int, bytes]:
    if len(raw) != RECORD_SIZE or raw[2:3] != b":" or raw[11:12] != b" ":
        raise InputError("树节点记录格式非法")
    try:
        depth = int(raw[0:2])
        index = int(raw[3:11])
        value = bytes.fromhex(raw[12:76].decode("ascii"))
    except ValueError as exc:
        raise InputError("树节点记录格式非法") from exc
    return depth, index, value


def build_header(branch: int, blocks: int, frozen: list[int]) -> bytes:
    frozen_field = " ".join(str(item) for item in frozen) if frozen else DASH
    return (
        "format 1\n"
        f"branch {branch}\n"
        f"blocks {blocks}\n"
        f"frozen {frozen_field}\n"
        "nodes\n"
    ).encode("utf-8")


class TreeHeader:
    def __init__(self, branch: int, blocks: int, frozen: list[int],
                 frozen_line: str, header_size: int):
        self.branch = branch
        self.blocks = blocks
        self.frozen = frozen
        self.frozen_line = frozen_line
        self.header_size = header_size
        self.depth = tree_depth(blocks, branch)
        self.counts = layer_counts(blocks, branch, self.depth)
        self.prefixes = prefix_offsets(self.counts)

    def header_bytes_for(self, blocks: int) -> bytes:
        return (
            "format 1\n"
            f"branch {self.branch}\n"
            f"blocks {blocks}\n"
            f"frozen {self.frozen_line}\n"
            "nodes\n"
        ).encode("utf-8")

    def record_offset(self, depth: int, index: int) -> int:
        return (
            self.header_size
            + RECORD_SIZE * (self.prefixes[depth] + index)
        )


def read_tree_header(path: str) -> TreeHeader:
    with open(path, "rb") as handle:
        raw = handle.read(512)
    lines = raw.split(b"\n", 5)
    if len(lines) < 6:
        raise InputError("树文件头不完整")
    text_lines = []
    header_size = 0
    for line in lines[:5]:
        try:
            text_lines.append(line.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise InputError("树文件头不是 UTF-8") from exc
        header_size += len(line) + 1
    if text_lines[0] != "format 1":
        raise InputError("树文件格式标识非法")
    try:
        if text_lines[1].split(" ")[0] != "branch":
            raise ValueError
        branch = int(text_lines[1].split(" ")[1])
        blocks = int(text_lines[2].split(" ")[1])
    except (ValueError, IndexError) as exc:
        raise InputError("树文件头 branch/blocks 非法") from exc
    if not (2 <= branch <= 64) or blocks < 0:
        raise InputError("树文件头 branch/blocks 超出范围")
    if text_lines[2] != f"blocks {blocks}":
        raise InputError("树文件头 blocks 行非法")
    if not text_lines[3].startswith("frozen "):
        raise InputError("树文件头 frozen 行非法")
    frozen_field = text_lines[3][len("frozen "):]
    frozen: list[int] = []
    if frozen_field != DASH:
        try:
            frozen = [int(part) for part in frozen_field.split(" ")]
        except ValueError as exc:
            raise InputError("树文件头 frozen 行非法") from exc
        if frozen != sorted(frozen) or len(set(frozen)) != len(frozen):
            raise InputError("树文件头 frozen 未按升序去重排列")
    if text_lines[4] != "nodes":
        raise InputError("树文件头缺少 nodes 标记")
    expected_size = header_size + RECORD_SIZE * sum(
        layer_counts(blocks, branch, tree_depth(blocks, branch))
    )
    actual_size = os.path.getsize(path)
    if actual_size != expected_size:
        raise InputError("树文件节点区长度与头信息不符")
    return TreeHeader(branch, blocks, frozen, frozen_field, header_size)


def blocklist_count(path: str) -> int:
    size = os.path.getsize(path)
    if size % BLOCKLIST_RECORD_SIZE != 0:
        raise InputError("清单长度不是定长记录的整数倍")
    return size // BLOCKLIST_RECORD_SIZE


def read_blocklist_digest(path: str, index: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(index * BLOCKLIST_RECORD_SIZE)
        raw = handle.read(BLOCKLIST_RECORD_SIZE)
    return parse_blocklist_record(raw, index)


def parse_blocklist_record(raw: bytes, expected_index: int) -> bytes:
    if len(raw) != BLOCKLIST_RECORD_SIZE or raw.count(b"\t") != 3 or raw[-1:] != b"\n":
        raise InputError(f"块 {expected_index} 清单记录格式非法")
    number_field, offset_field, length_field, digest_field = raw[:-1].split(b"\t")
    try:
        number = int(number_field)
        offset = int(offset_field)
        length = int(length_field)
        digest = bytes.fromhex(digest_field.decode("ascii"))
    except ValueError as exc:
        raise InputError(f"块 {expected_index} 清单记录格式非法") from exc
    if number != expected_index or offset != expected_index * BLOCK_SIZE:
        raise InputError(f"块 {expected_index} 位置信息非法")
    if length <= 0 or length > BLOCK_SIZE or len(digest) != 32:
        raise InputError(f"块 {expected_index} 清单记录格式非法")
    return digest


def iter_blocklist(path: str):
    """顺序产生 (块号, 摘要字节)，build 全量读取用。"""
    with open(path, "rb") as handle:
        index = 0
        while True:
            raw = handle.read(BLOCKLIST_RECORD_SIZE)
            if not raw:
                return
            yield index, parse_blocklist_record(raw, index)
            index += 1


class Change:
    __slots__ = ("block", "action", "old_digest", "new_digest")

    def __init__(self, block, action, old_digest, new_digest):
        self.block = block
        self.action = action
        self.old_digest = old_digest
        self.new_digest = new_digest


def _parse_digest_field(field: str) -> bytes:
    try:
        value = bytes.fromhex(field)
    except ValueError as exc:
        raise InputError("变更集摘要格式非法") from exc
    if len(value) != 32:
        raise InputError("变更集摘要长度非法")
    return value


def read_changes(path: str) -> list[Change]:
    changes: list[Change] = []
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw:
        return changes
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError("变更集不是 UTF-8") from exc
    if text.startswith("\ufeff") or not text.endswith("\n") or "\r" in text:
        raise InputError("变更集行尾或编码非法")
    for line in text.split("\n")[:-1]:
        parts = line.split("\t")
        if len(parts) != 4:
            raise InputError("变更集记录字段数非法")
        block_text, action, old_text, new_text = parts
        if action not in ACTIONS:
            raise InputError("变更集动作非法")
        try:
            block = int(block_text)
        except ValueError as exc:
            raise InputError("变更集块号非法") from exc
        if block < 0:
            raise InputError("变更集块号非法")
        old_digest = None if old_text == DASH else _parse_digest_field(old_text)
        new_digest = None if new_text == DASH else _parse_digest_field(new_text)
        if action == ACTION_REPLACE and (old_digest is None or new_digest is None):
            raise InputError("替换变更必须给出新旧摘要")
        if action == ACTION_APPEND and (old_digest is not None or new_digest is None):
            raise InputError("新增变更的摘要口径非法")
        if action == ACTION_DELETE and (old_digest is None or new_digest is not None):
            raise InputError("删除变更的摘要口径非法")
        changes.append(Change(block, action, old_digest, new_digest))
    return changes
