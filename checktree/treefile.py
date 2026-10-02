"""树文件（.tree）的头部解析与按坐标寻址读取（README 4.3）。"""
from __future__ import annotations

from .core import (
    TREE_RECORD,
    TREE_RECORD_HASH_OFFSET,
    layer_counts,
    prefix_sums,
)


class InputError(Exception):
    """输入不可用：文件缺失、格式不符等。"""


class TreeFile:
    """打开的树文件；只按需 seek 读取涉及的记录，不整树载入内存。"""

    def __init__(self, path, branch, blocks, frozen, frozen_text, header):
        self.path = path
        self.branch = branch
        self.blocks = blocks
        self.frozen = frozen
        self.frozen_text = frozen_text
        self.header = header
        self.counts = layer_counts(blocks, branch)
        self.prefix = prefix_sums(self.counts)
        self.data_offset = len(header)
        self._fh = open(path, "rb")

    @classmethod
    def open(cls, path):
        try:
            with open(path, "rb") as fh:
                lines = []
                for _ in range(5):
                    line = fh.readline()
                    if not line:
                        raise InputError(f"树文件头不完整: {path}")
                    lines.append(line)
        except FileNotFoundError:
            raise InputError(f"树文件不存在: {path}") from None
        header = b"".join(lines)
        try:
            text = [line.decode("utf-8").rstrip("\n") for line in lines]
        except UnicodeDecodeError:
            raise InputError(f"树文件不是 UTF-8: {path}") from None
        if text[0] != "format 1":
            raise InputError(f"树文件 format 行不符: {path}")
        try:
            branch = int(text[1].split(" ", 1)[1])
            blocks = int(text[2].split(" ", 1)[1])
        except (IndexError, ValueError):
            raise InputError(f"树文件头数字无法解析: {path}") from None
        if not text[1].startswith("branch ") or not 2 <= branch <= 64:
            raise InputError(f"树文件 branch 行不符: {path}")
        if not text[2].startswith("blocks ") or blocks < 0:
            raise InputError(f"树文件 blocks 行不符: {path}")
        if not text[3].startswith("frozen "):
            raise InputError(f"树文件 frozen 行不符: {path}")
        frozen_text = text[3].split(" ", 1)[1]
        if frozen_text == "-":
            frozen = ()
        else:
            try:
                frozen = tuple(int(item) for item in frozen_text.split(" "))
            except ValueError:
                raise InputError(f"树文件 frozen 块号无法解析: {path}") from None
        if text[4] != "nodes":
            raise InputError(f"树文件缺少 nodes 行: {path}")
        return cls(path, branch, blocks, frozen, frozen_text, header)

    def record_offset(self, layer: int, index: int) -> int:
        return self.data_offset + TREE_RECORD * (self.prefix[layer] + index)

    def read_node(self, layer: int, index: int) -> bytes:
        """读取节点 (layer, index) 的 32 字节哈希。"""
        self._fh.seek(self.record_offset(layer, index) + TREE_RECORD_HASH_OFFSET)
        hex_text = self._fh.read(64)
        if len(hex_text) != 64:
            raise InputError(f"树文件记录缺失: 层 {layer} 序号 {index}")
        return bytes.fromhex(hex_text.decode("ascii"))

    def close(self):
        self._fh.close()
