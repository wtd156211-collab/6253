"""端到端与单元测试；只读 samples/，临时文件都进 tempfile。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

sys.path.insert(0, str(ROOT))

from checktree.core import (  # noqa: E402
    block_path,
    layer_counts,
    leaf_hash,
    node_hash,
)

SAMPLE_NAMES = [
    "single-replace",
    "multi-frozen",
    "append-tail",
    "truncate-tail",
    "no-change",
    "stale-record",
    "apply-to-frozen",
]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "checktree", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def run_check_sample(name: str, out: Path, page: Path) -> subprocess.CompletedProcess:
    return run_cli(
        "check",
        "--tree", str(SAMPLES / "trees" / f"{name}.tree"),
        "--blocks", str(SAMPLES / "blocklists" / f"{name}.v2.tsv"),
        "--changes", str(SAMPLES / "blocklists" / f"{name}.changes.tsv"),
        "--out", str(out),
        "--page", str(page),
    )


def read_tree_records(path: Path) -> dict[tuple[int, int], str]:
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines()[5:]:
        coord, hash_hex = line.split(" ")
        layer, index = coord.split(":")
        records[(int(layer), int(index))] = hash_hex
    return records


class SampleCheckTests(unittest.TestCase):
    """逐行比对 samples/expected/<名>.check.txt 与 <名>.tree。"""

    def test_all_samples(self):
        for name in SAMPLE_NAMES:
            with self.subTest(name=name):
                expected_lines = (
                    (SAMPLES / "expected" / f"{name}.check.txt")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                expected_stdout = expected_lines[:-1]
                expected_code = int(expected_lines[-1].split(" ")[1])
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.tree"
                    page = Path(tmp) / "page.html"
                    proc = run_check_sample(name, out, page)
                    out_bytes = out.read_bytes() if out.exists() else None
                    page_exists = page.exists()
                self.assertEqual(proc.returncode, expected_code, proc.stderr)
                self.assertEqual(proc.stdout.splitlines(), expected_stdout)
                expected_tree = SAMPLES / "expected" / f"{name}.tree"
                if expected_tree.exists():
                    self.assertEqual(out_bytes, expected_tree.read_bytes())
                    self.assertTrue(page_exists)
                else:
                    self.assertIsNone(out_bytes)
                    self.assertFalse(page_exists)

    def test_failure_keeps_tree_untouched(self):
        for name in ("stale-record", "apply-to-frozen"):
            with self.subTest(name=name):
                before = (SAMPLES / "trees" / f"{name}.tree").read_bytes()
                with tempfile.TemporaryDirectory() as tmp:
                    proc = run_check_sample(name, Path(tmp) / "o.tree", Path(tmp) / "p.html")
                self.assertEqual(proc.returncode, 2)
                self.assertEqual((SAMPLES / "trees" / f"{name}.tree").read_bytes(), before)


class BuildTests(unittest.TestCase):
    """不带冻结的样例：build v1 清单应当逐字节得到 trees/<名>.tree。"""

    def test_build_matches_sample_trees(self):
        for name in SAMPLE_NAMES:
            tree_path = SAMPLES / "trees" / f"{name}.tree"
            header = tree_path.read_text(encoding="utf-8").splitlines()
            if header[3] != "frozen -":
                continue
            branch = header[1].split(" ")[1]
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "built.tree"
                proc = run_cli(
                    "build",
                    "--blocks", str(SAMPLES / "blocklists" / f"{name}.v1.tsv"),
                    "--tree", str(out),
                    "--branch", branch,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(out.read_bytes(), tree_path.read_bytes())
                lines = proc.stdout.splitlines()
                self.assertEqual(lines[0], f"块数 {header[2].split(' ')[1]}")
                layers = {record[0] for record in read_tree_records(tree_path)}
                self.assertEqual(lines[1], f"层数 {len(layers)}")
                self.assertTrue(lines[2].startswith("根 "))


class IncrementalTests(unittest.TestCase):
    """带冻结的样例：只有重算路径上的记录会变，其余记录一个字节不动。"""

    def test_only_recomputed_records_change(self):
        name = "multi-frozen"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.tree"
            proc = run_check_sample(name, out, Path(tmp) / "page.html")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            before = read_tree_records(SAMPLES / "trees" / f"{name}.tree")
            after = read_tree_records(out)
            recomputed_line = next(
                l for l in proc.stdout.splitlines() if l.startswith("重算节点 ")
            )
            recomputed = {
                (int(c.split(":")[0]), int(c.split(":")[1]))
                for c in recomputed_line.split(" ")[1:]
            }
            self.assertEqual(set(before), set(after))
            changed = {coord for coord in before if before[coord] != after[coord]}
            self.assertEqual(changed, recomputed)
            for coord, hash_hex in after.items():
                if coord not in recomputed:
                    self.assertEqual(hash_hex, before[coord])
            # 冻结块 0 与 16 的叶保持原值
            self.assertEqual(after[(0, 0)], before[(0, 0)])
            self.assertEqual(after[(0, 16)], before[(0, 16)])


class DeterminismTests(unittest.TestCase):
    """同一输入跑两遍：stdout、输出树、页面逐字节相同。"""

    def test_two_runs_identical(self):
        name = "multi-frozen"
        outputs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out.tree"
                page = Path(tmp) / "page.html"
                proc = run_check_sample(name, out, page)
                outputs.append((proc.stdout, out.read_bytes(), page.read_bytes()))
        self.assertEqual(outputs[0], outputs[1])


class PageTests(unittest.TestCase):
    """页面 4.5 三项：summary、tree 节点集合、边与计数来自引擎输出。"""

    def test_page_contents(self):
        name = "multi-frozen"
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            proc = run_check_sample(name, Path(tmp) / "out.tree", page)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html_text = page.read_text(encoding="utf-8")
        self.assertIn('<dl id="summary">', html_text)
        self.assertIn('<svg id="tree" viewBox="', html_text)
        lines = proc.stdout.splitlines()
        values = dict(line.split(" ", 1) for line in lines[1:6])
        self.assertIn(f"<dt>块数</dt><dd>{values['块数']}</dd>", html_text)
        self.assertIn(f"<dt>变化块</dt><dd>{values['变化块']}</dd>", html_text)
        self.assertIn(f"<dt>重算数</dt><dd>{values['重算数']}</dd>", html_text)
        self.assertIn(f"<dt>根</dt><dd>{values['根']}</dd>", html_text)
        recomputed = values["重算节点"].split(" ")
        for coord in recomputed:
            self.assertIn(f'data-node="{coord}"', html_text)
        self.assertEqual(html_text.count('data-node="'), int(values["重算数"]))
        self.assertEqual(html_text.count('class="node changed-block"'), 4)
        self.assertIn('data-edge="1:0/0:3"', html_text)


class CoreTests(unittest.TestCase):
    """哈希口径与坐标（样例树里的已知值）。"""

    def test_layer_counts(self):
        self.assertEqual(layer_counts(0, 4), [])
        self.assertEqual(layer_counts(1, 4), [1])
        self.assertEqual(layer_counts(12, 4), [12, 3, 1])
        self.assertEqual(layer_counts(17, 4), [17, 5, 2, 1])
        self.assertEqual(layer_counts(5, 2), [5, 3, 2, 1])

    def test_leaf_hash_matches_sample(self):
        digest = bytes.fromhex(
            "b816cfa7fc3295a78c7b7cd430ae3442555839befc39bad603a1332ebb3ab033"
        )
        self.assertEqual(
            leaf_hash(0, digest).hex(),
            "c449e45dadb190971f2ad408ac627f89c60f79297b0567994e95a31c8780dccb",
        )

    def test_node_hash_matches_sample(self):
        lines = (SAMPLES / "blocklists" / "single-replace.v1.tsv").read_bytes().splitlines()
        leaves = [
            leaf_hash(i, bytes.fromhex(line.split(b"\t")[3].decode("ascii")))
            for i, line in enumerate(lines[:4])
        ]
        self.assertEqual(
            node_hash(leaves).hex(),
            "6cbadc9ce73ce12b2144cebc60a29032d7a757d66d0168c9de6c0861358487b4",
        )

    def test_block_path(self):
        self.assertEqual(block_path(3, [17, 5, 2, 1], 4), "0/0/3")
        self.assertEqual(block_path(12, [17, 5, 2, 1], 4), "0/3/0")
        self.assertEqual(block_path(5, [6, 3, 2, 1], 2), "1/0/1")
        self.assertEqual(block_path(0, [1], 4), "-")


class RoundTripTests(unittest.TestCase):
    """合成数据：check --out 的树必须与对 v2 清单重新 build 的树逐字节一致。"""

    @staticmethod
    def _write_blocklist(path: Path, digests: list[str]) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for i, digest in enumerate(digests):
                fh.write(f"{i:08d}\t{i * 4096:012d}\t{4096:05d}\t{digest}\n")

    def test_roundtrip(self):
        import hashlib
        import random

        rng = random.Random(20261002)

        def digest(seed: int) -> str:
            return hashlib.sha256(f"block-{seed}".encode()).hexdigest()

        cases = [
            (4, 1, "replace"),
            (4, 37, "replace"),
            (2, 5, "append"),
            (3, 40, "truncate"),
            (4, 130, "append"),
            (4, 130, "truncate"),
        ]
        for branch, blocks, mode in cases:
            with self.subTest(branch=branch, blocks=blocks, mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    v1 = [digest(i) for i in range(blocks)]
                    v2 = list(v1)
                    change_lines = []
                    if mode == "replace":
                        targets = sorted(rng.sample(range(blocks), min(3, blocks)))
                        for block in targets:
                            v2[block] = digest(10_000 + block)
                            change_lines.append(f"{block}\t替换\t{v1[block]}\t{v2[block]}")
                    elif mode == "append":
                        v2.append(digest(20_000))
                        change_lines.append(f"{blocks}\t新增\t-\t{digest(20_000)}")
                    else:
                        change_lines.append(f"0\t替换\t{v1[0]}\t{digest(30_000)}")
                        v2[0] = digest(30_000)
                        change_lines.append(f"{blocks - 1}\t删除\t{v1[-1]}\t-")
                        v2 = v2[:-1]
                    v1_path = tmp_path / "v1.tsv"
                    v2_path = tmp_path / "v2.tsv"
                    changes_path = tmp_path / "c.tsv"
                    self._write_blocklist(v1_path, v1)
                    self._write_blocklist(v2_path, v2)
                    changes_path.write_text(
                        "".join(line + "\n" for line in change_lines), encoding="utf-8"
                    )
                    tree_path = tmp_path / "a.tree"
                    out_path = tmp_path / "b.tree"
                    fresh_path = tmp_path / "fresh.tree"
                    self.assertEqual(
                        run_cli("build", "--blocks", str(v1_path),
                                "--tree", str(tree_path), "--branch", str(branch)).returncode,
                        0,
                    )
                    proc = run_cli(
                        "check", "--tree", str(tree_path), "--blocks", str(v2_path),
                        "--changes", str(changes_path), "--out", str(out_path),
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(
                        run_cli("build", "--blocks", str(v2_path),
                                "--tree", str(fresh_path), "--branch", str(branch)).returncode,
                        0,
                    )
                    self.assertEqual(out_path.read_bytes(), fresh_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
