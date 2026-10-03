"""checktree 的 unittest 套件：只读 samples/，临时产物都进 tmp 目录。"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import pathlib
import random
import tempfile
import tracemalloc
import unittest

from checktree import cli
from checktree.core import (
    RECORD_SIZE,
    layer_counts,
    read_tree_header,
    tree_depth,
)
from checktree.engine import build_tree

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def run_cli(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def sample_names():
    return sorted(
        p.name[: -len(".check.txt")]
        for p in (SAMPLES / "expected").glob("*.check.txt")
    )


def expected_stdout_and_code(name):
    lines = (SAMPLES / "expected" / f"{name}.check.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert lines[-1].startswith("退出码 ")
    return lines[:-1], int(lines[-1].split(" ")[1])


def check_args(name, out_tree=None, page=None):
    argv = [
        "check",
        "--tree", str(SAMPLES / "trees" / f"{name}.tree"),
        "--blocks", str(SAMPLES / "blocklists" / f"{name}.v2.tsv"),
        "--changes", str(SAMPLES / "blocklists" / f"{name}.changes.tsv"),
    ]
    if out_tree is not None:
        argv += ["--out", out_tree]
    if page is not None:
        argv += ["--page", page]
    return argv


def tree_records(path):
    data = pathlib.Path(path).read_bytes()
    header = read_tree_header(path)
    records = {}
    pos = header.header_size
    while pos < len(data):
        raw = data[pos: pos + RECORD_SIZE]
        depth = int(raw[0:2])
        index = int(raw[3:11])
        records[(depth, index)] = raw
        pos += RECORD_SIZE
    return header, records


class SampleCheckTest(unittest.TestCase):
    """逐行比对 samples/expected 的 stdout、退出码与输出树。"""

    def test_samples(self):
        for name in sample_names():
            with self.subTest(sample=name), tempfile.TemporaryDirectory() as td:
                expected_lines, expected_code = expected_stdout_and_code(name)
                tree_path = SAMPLES / "trees" / f"{name}.tree"
                before = tree_path.read_bytes()
                out_tree = os.path.join(td, "out.tree")
                page = os.path.join(td, "page.html")
                code, stdout, _ = run_cli(
                    check_args(name, out_tree=out_tree, page=page))
                self.assertEqual(code, expected_code)
                self.assertEqual(stdout.splitlines(), expected_lines)
                self.assertEqual(tree_path.read_bytes(), before,
                                 "原树一个字节都不许动")
                expected_tree = SAMPLES / "expected" / f"{name}.tree"
                if expected_tree.exists():
                    self.assertEqual(
                        pathlib.Path(out_tree).read_bytes(),
                        expected_tree.read_bytes(),
                        "--out 写出的树必须逐字节相同",
                    )
                if code == 0:
                    self.assertTrue(os.path.exists(page))
                else:
                    self.assertFalse(os.path.exists(out_tree))
                    self.assertFalse(os.path.exists(page))


class BuildTest(unittest.TestCase):
    """不带冻结的样例：build 出的树与 samples/trees 逐字节相同。"""

    def test_build_matches_sample_trees(self):
        for name in sample_names():
            header = read_tree_header(SAMPLES / "trees" / f"{name}.tree")
            if header.frozen:
                continue
            with self.subTest(sample=name), tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "built.tree")
                code, stdout, _ = run_cli([
                    "build",
                    "--blocks", str(SAMPLES / "blocklists" / f"{name}.v1.tsv"),
                    "--tree", out,
                    "--branch", str(header.branch),
                ])
                self.assertEqual(code, 0)
                self.assertEqual(
                    pathlib.Path(out).read_bytes(),
                    (SAMPLES / "trees" / f"{name}.tree").read_bytes(),
                )
                lines = stdout.splitlines()
                self.assertEqual(lines[0], f"块数 {header.blocks}")
                self.assertEqual(lines[1], f"层数 {header.depth + 1}")
                _, records = tree_records(out)
                root = records[(header.depth, 0)]
                self.assertEqual(lines[2], f"根 {root[12:76].decode()}")


class DeterminismTest(unittest.TestCase):
    def test_same_input_same_output(self):
        outputs = []
        with tempfile.TemporaryDirectory() as td:
            for run in range(2):
                out_tree = os.path.join(td, f"out{run}.tree")
                page = os.path.join(td, f"page{run}.html")
                code, stdout, _ = run_cli(check_args(
                    "multi-frozen", out_tree=out_tree, page=page))
                outputs.append((
                    code, stdout,
                    pathlib.Path(out_tree).read_bytes(),
                    pathlib.Path(page).read_bytes(),
                ))
        self.assertEqual(outputs[0], outputs[1])


class FrozenPreservationTest(unittest.TestCase):
    def test_only_recomputed_records_change(self):
        with tempfile.TemporaryDirectory() as td:
            out_tree = os.path.join(td, "out.tree")
            code, stdout, _ = run_cli(
                check_args("multi-frozen", out_tree=out_tree))
            self.assertEqual(code, 0)
            _, old_records = tree_records(SAMPLES / "trees" / "multi-frozen.tree")
            _, new_records = tree_records(out_tree)
            self.assertEqual(old_records.keys(), new_records.keys())
            recomputed_line = next(
                ln for ln in stdout.splitlines() if ln.startswith("重算节点 "))
            recomputed = {
                tuple(int(part) for part in coord.split(":"))
                for coord in recomputed_line.split(" ")[1:]
            }
            changed = {
                coord for coord in old_records
                if old_records[coord] != new_records[coord]
            }
            self.assertEqual(changed, recomputed)
            header = read_tree_header(SAMPLES / "trees" / "multi-frozen.tree")
            for block in header.frozen:
                self.assertEqual(old_records[(0, block)],
                                 new_records[(0, block)])


class PageTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.page = os.path.join(self.td.name, "page.html")
        code, stdout, _ = run_cli(check_args("multi-frozen", page=self.page))
        assert code == 0
        self.stdout_lines = stdout.splitlines()
        self.html = pathlib.Path(self.page).read_text(encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_summary_matches_stdout(self):
        self.assertIn('<dl id="summary">', self.html)
        values = dict(
            line.split(" ", 1) for line in self.stdout_lines
            if line.split(" ", 1)[0] in ("块数", "变化块", "重算数", "根")
        )
        for name, value in values.items():
            self.assertIn(f"<dt>{name}</dt><dd>{value}</dd>", self.html)

    def test_tree_svg_nodes_match_recomputed(self):
        self.assertIn('<svg id="tree" viewBox="', self.html)
        recomputed_line = next(
            ln for ln in self.stdout_lines if ln.startswith("重算节点 "))
        coords = recomputed_line.split(" ")[1:]
        for coord in coords:
            self.assertIn(f'data-node="{coord}"', self.html)
        self.assertEqual(self.html.count('data-node="'), len(coords))

    def test_changed_blocks_marked_red(self):
        for block in ("3", "5", "9", "12"):
            self.assertIn(
                f'class="node changed-block" data-node="0:{block}"', self.html)
        self.assertIn('class="node changed" data-node="1:0"', self.html)
        self.assertIn("data-edge=", self.html)

    def test_self_contained(self):
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<script", self.html)


def make_blocklist(path, count, seed):
    rng = random.Random(seed)
    with open(path, "w") as handle:
        for index in range(count):
            digest = hashlib.sha256(rng.randbytes(48)).hexdigest()
            handle.write(
                f"{index:08d}\t{index * 4096:012d}\t{4096:05d}\t{digest}\n")


def read_digests(path):
    with open(path) as handle:
        return [line.split("\t")[3].strip() for line in handle]


class SyntheticTreeTest(unittest.TestCase):
    """临时目录里造数据：核对增量结果与全量重建一致。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        td = self.td.name
        self.count = 30_000
        self.branch = 4
        self.bl1 = os.path.join(td, "v1.tsv")
        self.tree = os.path.join(td, "v1.tree")
        make_blocklist(self.bl1, self.count, seed=7)
        build_tree(self.bl1, self.tree, self.branch)
        self.digests = read_digests(self.bl1)

    def tearDown(self):
        self.td.cleanup()

    def apply_changes(self, changes, new_count):
        td = self.td.name
        new_digests = list(self.digests[:min(new_count, self.count)])
        new_digests += ["0" * 64] * (new_count - len(new_digests))
        ch_path = os.path.join(td, "round.changes.tsv")
        with open(ch_path, "w") as handle:
            for block, action, new_digest in sorted(
                    changes, key=lambda item: item[0]):
                if action == "替换":
                    old = self.digests[block]
                    new_digests[block] = new_digest
                    handle.write(f"{block}\t替换\t{old}\t{new_digest}\n")
                elif action == "新增":
                    new_digests[block] = new_digest
                    handle.write(f"{block}\t新增\t-\t{new_digest}\n")
                else:
                    handle.write(
                        f"{block}\t删除\t{self.digests[block]}\t-\n")
        bl2 = os.path.join(td, "v2.tsv")
        with open(bl2, "w") as handle:
            for index, digest in enumerate(new_digests):
                handle.write(
                    f"{index:08d}\t{index * 4096:012d}\t{4096:05d}\t{digest}\n")
        return ch_path, bl2

    def run_check(self, ch_path, bl2):
        out_tree = os.path.join(self.td.name, "v2.tree")
        page = os.path.join(self.td.name, "page.html")
        code, stdout, _ = run_cli([
            "check", "--tree", self.tree, "--blocks", bl2,
            "--changes", ch_path, "--out", out_tree, "--page", page,
        ])
        self.assertEqual(code, 0)
        return stdout.splitlines(), out_tree

    def test_replace_append_delete_against_full_rebuild(self):
        rng = random.Random(11)
        cases = [
            ("replace", [(b, "替换", hashlib.sha256(f"r{b}".encode()).hexdigest())
                         for b in sorted(rng.sample(range(self.count), 50))],
             self.count),
            ("append", [(self.count + i, "新增",
                         hashlib.sha256(f"a{i}".encode()).hexdigest())
                        for i in range(7)], self.count + 7),
            ("delete", [(self.count - 1 - i, "删除", None) for i in range(9)]
             + [(3, "替换", hashlib.sha256(b"x").hexdigest())], self.count - 9),
        ]
        for tag, changes, new_count in cases:
            with self.subTest(case=tag):
                ch_path, bl2 = self.apply_changes(changes, new_count)
                lines, out_tree = self.run_check(ch_path, bl2)
                rebuilt = os.path.join(self.td.name, f"rebuilt-{tag}.tree")
                build_tree(bl2, rebuilt, self.branch)
                _, expected_records = tree_records(rebuilt)
                _, actual_records = tree_records(out_tree)
                self.assertEqual(actual_records, expected_records,
                                 "增量写出的树必须等于全量重建")
                recomputed_line = next(
                    ln for ln in lines if ln.startswith("重算节点 "))
                recomputed_count = int(next(
                    ln.split(" ")[1] for ln in lines
                    if ln.startswith("重算数 ")))
                depth = tree_depth(new_count, self.branch)
                self.assertLessEqual(recomputed_count,
                                     len(changes) * (depth + 1))
                if recomputed_line != "重算节点 -":
                    self.assertEqual(
                        len(recomputed_line.split(" ")) - 1, recomputed_count)

    def test_untouched_records_byte_identical(self):
        changes = [(100, "替换", hashlib.sha256(b"c100").hexdigest()),
                   (20000, "替换", hashlib.sha256(b"c20000").hexdigest())]
        ch_path, bl2 = self.apply_changes(changes, self.count)
        lines, out_tree = self.run_check(ch_path, bl2)
        _, old_records = tree_records(self.tree)
        _, new_records = tree_records(out_tree)
        recomputed_line = next(
            ln for ln in lines if ln.startswith("重算节点 "))
        recomputed = {
            tuple(int(part) for part in coord.split(":"))
            for coord in recomputed_line.split(" ")[1:]
        }
        for coord, raw in old_records.items():
            if coord not in recomputed:
                self.assertEqual(raw, new_records[coord])

    def test_memory_independent_of_block_count(self):
        peaks = []
        for count in (100_000, 400_000):
            td = tempfile.TemporaryDirectory()
            bl = os.path.join(td.name, "v1.tsv")
            tree = os.path.join(td.name, "v1.tree")
            make_blocklist(bl, count, seed=3)
            build_tree(bl, tree, 4)
            digests = read_digests(bl)
            rng = random.Random(5)
            picks = sorted(rng.sample(range(count), 200))
            ch_path = os.path.join(td.name, "ch.tsv")
            new_digests = list(digests)
            with open(ch_path, "w") as handle:
                for block in picks:
                    new = hashlib.sha256(f"m{block}".encode()).hexdigest()
                    new_digests[block] = new
                    handle.write(f"{block}\t替换\t{digests[block]}\t{new}\n")
            bl2 = os.path.join(td.name, "v2.tsv")
            with open(bl2, "w") as handle:
                for index, digest in enumerate(new_digests):
                    handle.write(
                        f"{index:08d}\t{index * 4096:012d}\t{4096:05d}"
                        f"\t{digest}\n")
            tracemalloc.start()
            run_cli(["check", "--tree", tree, "--blocks", bl2,
                     "--changes", ch_path,
                     "--out", os.path.join(td.name, "out.tree"),
                     "--page", os.path.join(td.name, "p.html")])
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            td.cleanup()
        self.assertLessEqual(abs(peaks[1] - peaks[0]), 32 * 2**20)


if __name__ == "__main__":
    unittest.main()
