"""哈希口径与树形布局（README 第 2 节）。"""
from __future__ import annotations

import hashlib

BLOCK_SIZE = 4096
BLOCKLIST_RECORD = 93
BLOCKLIST_DIGEST_OFFSET = 28
BLOCKLIST_DIGEST_LENGTH = 64
TREE_RECORD = 77
TREE_RECORD_HASH_OFFSET = 12

EMPTY_ROOT = hashlib.sha256(b"").hexdigest()


def leaf_hash(index: int, digest: bytes) -> bytes:
    """H(0, i) = sha256(0x00 | u64be(i) | 摘要字节)。"""
    return hashlib.sha256(b"\x00" + index.to_bytes(8, "big") + digest).digest()


def node_hash(children: list[bytes]) -> bytes:
    """H(d, j) = sha256(0x01 | u64be(子数) | 子哈希按序号拼接)。"""
    hasher = hashlib.sha256()
    hasher.update(b"\x01")
    hasher.update(len(children).to_bytes(8, "big"))
    for child in children:
        hasher.update(child)
    return hasher.digest()


def layer_counts(blocks: int, branch: int) -> list[int]:
    """各层节点数，第 0 层是叶；空清单没有层。"""
    if blocks <= 0:
        return []
    counts = [blocks]
    while counts[-1] > 1:
        counts.append((counts[-1] + branch - 1) // branch)
    return counts


def prefix_sums(counts: list[int]) -> list[int]:
    """前 d 层节点总数，用于定位记录。"""
    out = []
    total = 0
    for count in counts:
        out.append(total)
        total += count
    return out


def block_path(block: int, counts: list[int], branch: int) -> str:
    """根到叶的子下标序列，从根那头写起；只有根时写 '-'。"""
    depth = len(counts) - 1
    if depth <= 0:
        return "-"
    parts = []
    index = block
    for _ in range(depth):
        parts.append(str(index % branch))
        index //= branch
    return "/".join(reversed(parts))


def format_record(layer: int, index: int, hash_bytes: bytes) -> bytes:
    """定长 77 字节节点记录：层(2) : 序号(8) 空格 哈希(64) 换行。"""
    return f"{layer:02d}:{index:08d} {hash_bytes.hex()}\n".encode("ascii")
