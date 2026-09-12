#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hosted-replay.py 单元测试（unittest，纯标准库，全部使用合成 fixture）。

跑法：
    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_hosted_replay -v

覆盖：分页翻页（sessions 2 页 / session 详情 2 页）、file-tree 重建
（write args 的 JSON 字符串与 dict 两种形状 + 路径清洗）、timeline 行数、
usage-summary 聚合与 null 处理、公开模式路径前缀切换、HTTP 错误重试与单会话失败不中断。

注意：所有 fixture 均为合成的占位内容，不含任何真实题目/解题内容。
"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "hosted-replay.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("hosted_replay", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hr = _load_module()


# --------------------------------------------------------------------------
# 合成 fixture 构造
# --------------------------------------------------------------------------

def make_item(role="assistant", kind="text", name=None, text="", char_len=None,
              args=None, is_error=False):
    item = {"role": role, "kind": kind, "name": name, "call_id": None,
            "args": args, "text": text, "fp": None, "is_error": is_error}
    item["char_len"] = len(text) if char_len is None else char_len
    return item


def make_step(seq, captured_at, items, usage=None, event_type="exchange"):
    return {"id": "st-%s" % seq, "seq": seq, "captured_at": captured_at,
            "event_type": event_type, "state": "ok", "stop_reason": None,
            "render_state": None, "error": None, "raw_exchange_id": None,
            "usage": usage, "items": items}


def make_session(sid, model="synthetic-model-a", usage=None, **extra):
    session = {"id": sid, "group_id": "g-1", "task_code": "SYN-001", "model": model,
               "protocol": "openai", "status": "closed", "closed_reason": None,
               "first_captured_at": "2026-01-01T00:00:00+00:00",
               "last_active_at": "2026-01-01T01:00:00+00:00",
               "event_count": 3, "range_call_count": 1, "explicit_id": sid,
               "title": "synthetic session %s" % sid,
               "working_directory": "/home/user/work/repo", "usage": usage}
    session.update(extra)
    return session


def page(items, page_no=1, page_size=50, total_pages=1, key="items"):
    """构造带 pagination 的列表响应。"""
    resp = {key: items, "pagination": {"page": page_no, "page_size": page_size,
                                       "total": len(items), "total_pages": total_pages}}
    return resp


class RecordingTransport(hr.Transport):
    """包装一个 page 提供者，记录请求序列，用于断言翻页行为。"""

    kind = "recording"

    def __init__(self, sessions_pages, detail_pages):
        self.sessions_pages = sessions_pages
        self.detail_pages = detail_pages
        self.session_calls = []
        self.detail_calls = []

    def sessions_page(self, run_id, page_no, page_size):
        self.session_calls.append((run_id, page_no, page_size))
        if page_no - 1 < len(self.sessions_pages):
            return self.sessions_pages[page_no - 1]
        return page([], page_no, page_size, max(len(self.sessions_pages), 1))

    def session_page(self, run_id, sid, frm, to, page_no, page_size):
        self.detail_calls.append({"run_id": run_id, "sid": sid, "from": frm, "to": to,
                                  "page": page_no, "page_size": page_size})
        pages = self.detail_pages.get(sid, [])
        if page_no - 1 < len(pages):
            return pages[page_no - 1]
        return page([], page_no, page_size, max(len(pages), 1), key="steps")


class FakeResponse(object):
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class FakeUrlopen(object):
    """按 URL 回放响应；记录每个请求的完整 URL 与头。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.headers = []

    def __call__(self, req, timeout=None):
        self.requests.append(req.full_url)
        self.headers.append(dict(req.header_items()))
        return FakeResponse(self.handler(req.full_url))


class TempFixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hosted-replay-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_fixture(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def read_text(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def read_lines(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# 1) 分页翻页
# --------------------------------------------------------------------------

class TestPagination(TempFixtureCase):

    def test_sessions_two_pages(self):
        """sessions 列表 2 页 -> 全部拿到，且只请求到 total_pages。"""
        s1 = make_session("s-1")
        s2 = make_session("s-2")
        s3 = make_session("s-3")
        transport = RecordingTransport(
            [page([s1, s2], 1, 2, total_pages=2), page([s3], 2, 2, total_pages=2)], {})

        sessions, raws = hr.fetch_sessions(transport, "run-x", page_size=2)

        self.assertEqual([s["id"] for s in sessions], ["s-1", "s-2", "s-3"])
        self.assertEqual([c[1] for c in transport.session_calls], [1, 2])
        self.assertEqual(len(raws), 2)

    def test_sessions_stop_at_total_pages_even_if_page_full(self):
        """满页但 total_pages=1 -> 不多翻一页。"""
        transport = RecordingTransport(
            [page([make_session("s-1"), make_session("s-2")], 1, 2, total_pages=1)], {})

        sessions, _ = hr.fetch_sessions(transport, "run-x", page_size=2)

        self.assertEqual(len(sessions), 2)
        self.assertEqual(len(transport.session_calls), 1)

    def test_pagination_without_metadata_uses_short_page(self):
        """无 pagination.total_pages -> 用“本页不足 page_size”判定末页。"""
        transport = RecordingTransport([
            {"items": [make_session("s-1"), make_session("s-2")]},   # 满页 -> 继续
            {"items": [make_session("s-3")]},                        # 短页 -> 停
        ], {})

        sessions, _ = hr.fetch_sessions(transport, "run-x", page_size=2)

        self.assertEqual(len(sessions), 3)
        self.assertEqual([c[1] for c in transport.session_calls], [1, 2])

    def test_session_detail_two_pages(self):
        """会话详情 steps 2 页 -> 全部 steps，且 from/to 带上 60s 余量。"""
        session = make_session("s-1")
        steps_p1 = [make_step(1, "2026-01-01T00:00:10+00:00",
                              [make_item(text="first")])]
        steps_p2 = [make_step(2, "2026-01-01T00:30:00+00:00",
                              [make_item(text="second")]),
                    make_step(3, "2026-01-01T00:40:00+00:00",
                              [make_item(text="third")])]
        transport = RecordingTransport(
            [], {"s-1": [page(steps_p1, 1, 1, total_pages=2, key="steps"),
                         page(steps_p2, 2, 1, total_pages=2, key="steps")]})

        steps, detail, warnings = hr.fetch_session_detail(transport, session, "run-x",
                                                          page_size=1)

        self.assertEqual([s["seq"] for s in steps], [1, 2, 3])
        self.assertEqual([c["page"] for c in transport.detail_calls], [1, 2])
        self.assertEqual(warnings, [])

        call = transport.detail_calls[0]
        first = hr.parse_iso(call["from"])
        last = hr.parse_iso(call["to"])
        self.assertEqual(first.isoformat(), "2025-12-31T23:59:00+00:00")  # -60s
        self.assertEqual(last.isoformat(), "2026-01-01T01:01:00+00:00")   # +60s
        self.assertIsNone(detail.get("model"))

    def test_session_detail_merges_detail_session_and_warns_on_bad_times(self):
        session = make_session("s-9", model=None,
                               first_captured_at="not-a-time",
                               last_active_at=None)
        pages = [dict(page([make_step(1, "2026-01-01T00:00:00+00:00",
                                      [make_item(text="x")])], key="steps"),
                      session={"id": "s-9", "model": "synthetic-model-b"})]
        transport = RecordingTransport([], {"s-9": pages})

        steps, detail, warnings = hr.fetch_session_detail(transport, session, "run-x")

        self.assertEqual(len(steps), 1)
        self.assertEqual(detail.get("model"), "synthetic-model-b")
        self.assertTrue(any("first_captured_at" in w for w in warnings))
        self.assertIsNone(transport.detail_calls[0]["from"])
        self.assertIsNone(transport.detail_calls[0]["to"])

    def test_collect_pages_breaks_on_empty_page(self):
        """服务端不合逻辑（空页却声称还有更多）不死循环。"""
        calls = []

        def fetch(page_no, page_size):
            calls.append(page_no)
            return {"items": [], "pagination": {"page": page_no, "total_pages": 99}}

        items, _ = hr.collect_pages(fetch, "items", page_size=2)

        self.assertEqual(items, [])
        self.assertEqual(calls, [1])


# --------------------------------------------------------------------------
# 2) file-tree 重建
# --------------------------------------------------------------------------

class TestFileTree(unittest.TestCase):

    def test_write_args_json_string_and_dict_shapes(self):
        """write 的 args 两种形状（JSON 字符串 / dict）都能解析出路径与内容。"""
        steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="tool-call", name="write", role="assistant",
                          args=json.dumps({"file_path": "src/alpha.txt",
                                           "content": "AAAAA"})),
            ]),
            make_step(2, "2026-01-01T00:00:02+00:00", [
                make_item(kind="tool-call", name="write", role="assistant",
                          args={"path": "src/beta.txt", "content": "BBBBBBBB"}),
            ]),
        ]
        tree = hr.build_file_tree([(make_session("s-1"), steps)])

        self.assertEqual(sorted(tree["files"]), ["src/alpha.txt", "src/beta.txt"])
        self.assertEqual(tree["files"]["src/alpha.txt"]["char_count"], 5)
        self.assertEqual(tree["files"]["src/alpha.txt"]["source_sid"], "s-1")
        self.assertEqual(tree["files"]["src/alpha.txt"]["source_step_seq"], 1)
        self.assertEqual(tree["files"]["src/beta.txt"]["char_count"], 8)
        self.assertEqual(len(tree["write_events"]), 2)

    def test_path_cleaning_variants(self):
        """绝对路径去掉 working_directory 前缀；../ 与盘符被清洗。"""
        wd = "/home/user/work/repo"
        steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="tool-call", name="write", args=json.dumps(
                    {"file_path": "/home/user/work/repo/src/abs.txt", "content": "A"})),
            ]),
            make_step(2, "2026-01-01T00:00:02+00:00", [
                make_item(kind="tool-call", name="write", args={
                    "file_path": "./src/../src/dot.txt", "content": "BB"}),
            ]),
            make_step(3, "2026-01-01T00:00:03+00:00", [
                make_item(kind="tool-call", name="write",
                          args={"path": "C:\\repo\\win.txt", "content": "CCC"}),
            ]),
            make_step(4, "2026-01-01T00:00:04+00:00", [
                make_item(kind="tool-call", name="write",
                          args={"file_path": "../../escape.txt", "content": "D"}),
            ]),
            make_step(5, "2026-01-01T00:00:05+00:00", [
                make_item(kind="tool-call", name="write",
                          args={"file_path": "/", "content": "nope"}),
            ]),
        ]
        tree = hr.build_file_tree([(make_session("s-1", working_directory=wd), steps)])

        self.assertEqual(sorted(tree["files"]),
                         ["escape.txt", "repo/win.txt", "src/abs.txt", "src/dot.txt"])
        self.assertEqual(tree["files"]["src/abs.txt"]["source_sid"], "s-1")

    def test_last_write_wins(self):
        """同一路径多次 write -> files 记最后一次（时间较新者）。"""
        steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="tool-call", name="write",
                          args={"file_path": "a.txt", "content": "12345"})]),
            make_step(2, "2026-01-01T00:00:09+00:00", [
                make_item(kind="tool-call", name="write",
                          args={"file_path": "a.txt", "content": "1234567890"})]),
            make_step(3, "2026-01-01T00:00:05+00:00", [
                make_item(kind="tool-call", name="write",
                          args={"file_path": "a.txt", "content": "1"})]),
        ]
        tree = hr.build_file_tree([(make_session("s-1"), steps)])

        self.assertEqual(tree["files"]["a.txt"]["char_count"], 10)
        self.assertEqual(tree["files"]["a.txt"]["source_step_seq"], 2)
        self.assertEqual(tree["files"]["a.txt"]["last_captured_at"],
                         "2026-01-01T00:00:09+00:00")

    def test_read_events_and_edit_without_content(self):
        """read 记事件；edit 无完整内容只记事件、不写 files。"""
        steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="tool-call", name="read", role="assistant",
                          args=json.dumps({"file_path": "src/alpha.txt"})),
            ]),
            make_step(2, "2026-01-01T00:00:02+00:00", [
                make_item(kind="tool-call", name="edit", role="assistant",
                          args={"file_path": "src/alpha.txt",
                                "old_string": "AAA", "new_string": "BBB"}),
            ]),
            make_step(3, "2026-01-01T00:00:03+00:00", [
                make_item(kind="tool-call", name="edit", role="assistant",
                          args={"file_path": "src/gamma.txt", "content": "partial"}),
            ]),
        ]
        tree = hr.build_file_tree([(make_session("s-1"), steps)])

        self.assertEqual(tree["files"].get("src/alpha.txt"), None)
        self.assertEqual(sorted(tree["files"]), ["src/gamma.txt"])
        self.assertEqual(tree["files"]["src/gamma.txt"]["char_count"], 7)
        self.assertEqual([e["path"] for e in tree["read_events"]], ["src/alpha.txt"])
        self.assertEqual(tree["read_events"][0]["sid"], "s-1")
        incomplete = [e for e in tree["write_events"] if e["tool"] == "edit"
                      and not e["complete"]]
        self.assertEqual([e["path"] for e in incomplete], ["src/alpha.txt"])

    def test_non_tool_items_and_bad_args_ignored(self):
        """非 tool-call item、args 为 null / 非法 JSON 都不崩。"""
        steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="text", text="thinking aloud"),
                make_item(kind="tool-call", name="write", args=None),
                make_item(kind="tool-call", name="write", args="{not json"),
                make_item(kind="tool-call", name="write", args={"content": "no path"}),
                make_item(kind="tool-call", name="other", args={"file_path": "x.txt"}),
            ]),
        ]
        tree = hr.build_file_tree([(make_session("s-1"), steps)])

        self.assertEqual(tree["files"], {})
        self.assertEqual(tree["read_events"], [])
        self.assertEqual(tree["write_events"], [])


class TestCleanPath(unittest.TestCase):

    def test_clean_path_cases(self):
        cases = [
            ("src/a.py", None, "src/a.py"),
            ("./src/a.py", None, "src/a.py"),
            ("/abs/path/a.py", None, "abs/path/a.py"),
            ("../../etc/passwd", None, "etc/passwd"),
            ("a/../../b.txt", None, "b.txt"),
            ("/home/u/repo/src/a.py", "/home/u/repo", "src/a.py"),
            ("C:\\repo\\a.py", None, "repo/a.py"),
            ("  src/spaced.txt  ", None, "src/spaced.txt"),
            ("/", None, None),
            ("", None, None),
            ("..", None, None),
            (None, None, None),
            ("/home/u/repo", "/home/u/repo", None),
        ]
        for raw, wd, expected in cases:
            self.assertEqual(hr.clean_path(raw, wd), expected,
                             "clean_path(%r, %r)" % (raw, wd))


# --------------------------------------------------------------------------
# 3) timeline 行数
# --------------------------------------------------------------------------

class TestTimeline(TempFixtureCase):

    def _sessions_steps(self):
        s1_steps = [
            make_step(1, "2026-01-01T00:00:01+00:00",
                      [make_item(text="one"), make_item(kind="reasoning", text="two")],
                      usage={"input": 10, "output": 5}),
            make_step(2, "2026-01-01T00:00:02+00:00",
                      [make_item(kind="tool-call", name="read", args={"file_path": "a"})]),
        ]
        s2_steps = [
            make_step(1, "2026-01-01T00:10:00+00:00",
                      [make_item(role="user", text="ping")]),
        ]
        return [(make_session("s-1"), s1_steps), (make_session("s-2"), s2_steps)]

    def test_timeline_lines_equal_item_count(self):
        sessions_steps = self._sessions_steps()
        expected = sum(len(hr.step_items(st)) for _, steps in sessions_steps
                       for st in steps)
        rows = hr.build_timeline(sessions_steps)

        outdir = os.path.join(self.tmp, "out")
        hr.ensure_outdir(outdir)
        path = hr.write_timeline(outdir, rows)

        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), expected)
        self.assertEqual(len(lines), 4)
        for ln in lines:
            json.loads(ln)

    def test_timeline_row_fields_and_usage_normalization(self):
        rows = hr.build_timeline([(make_session("s-1"), [
            make_step(7, "2026-01-01T00:00:01+00:00",
                      [make_item(text="x" * 500)],
                      usage={"input": 10, "output": None, "reasoning": "junk"})])])
        row = rows[0]

        self.assertEqual(row["sid"], "s-1")
        self.assertEqual(row["step_seq"], 7)
        self.assertEqual(row["char_len"], 500)
        self.assertEqual(len(row["text_head"]), 200)
        self.assertEqual(row["step_usage"], {"cache_read": 0, "cache_write": 0,
                                             "input": 10, "output": 0, "reasoning": 0})

    def test_timeline_tool_call_falls_back_to_args(self):
        rows = hr.build_timeline([(make_session("s-1"), [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="tool-call", name="write", text="",
                          args={"file_path": "a.txt", "content": "zz"})])])])

        self.assertIn("a.txt", rows[0]["text_head"])
        self.assertEqual(rows[0]["char_len"], 0)  # char_len 缺失且无 text


# --------------------------------------------------------------------------
# 4) usage-summary 聚合
# --------------------------------------------------------------------------

class TestUsageSummary(unittest.TestCase):

    def _sessions_steps(self):
        s1 = make_session("s-1", model="model-a",
                          usage={"input": 100, "output": 50, "cache_read": None,
                                 "cache_write": 7, "reasoning": 3})
        s2 = make_session("s-2", model="model-b", usage={"input": 10})
        s3 = make_session("s-3", model=None, usage=None)
        s1_steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [make_item(text="a")],
                      usage={"input": 10, "output": 4, "cache_read": 1,
                             "cache_write": None, "reasoning": 2}),
            make_step(2, "2026-01-01T00:00:02+00:00", [make_item(text="b")],
                      usage=None),
        ]
        s2_steps = [
            make_step(1, "2026-01-01T00:10:00+00:00", [make_item(text="c")],
                      usage={"input": None, "output": 5, "cache_read": 2,
                             "cache_write": 0, "reasoning": None}),
        ]
        return [(s1, s1_steps), (s2, s2_steps), (s3, [])]

    def test_usage_aggregation_and_null_handling(self):
        summary = hr.build_usage_summary("run-x", self._sessions_steps())

        self.assertEqual(summary["run_id"], "run-x")
        self.assertEqual(summary["sessions"], 3)
        self.assertEqual(summary["steps"], 3)
        self.assertEqual(sorted(summary["by_model"]), ["model-a", "model-b", "unattributed"])

        a = summary["by_model"]["model-a"]
        self.assertEqual((a["input"], a["output"], a["cache_read"], a["cache_write"],
                          a["reasoning"]), (10, 4, 1, 0, 2))
        self.assertEqual((a["sessions"], a["steps"]), (1, 2))

        b = summary["by_model"]["model-b"]
        self.assertEqual((b["input"], b["output"], b["cache_read"], b["cache_write"],
                          b["reasoning"]), (0, 5, 2, 0, 0))

        # 只有 session 层 usage、无 step 的会话：计 session 数、不计 step
        u = summary["by_model"]["unattributed"]
        self.assertEqual((u["sessions"], u["steps"], u["input"]), (1, 0, 0))

        # total 来自 session.usage（仅作参考，不与 step 叠加）
        self.assertEqual(summary["total"], {"cache_read": 0, "cache_write": 7,
                                            "input": 110, "output": 50, "reasoning": 3})
        self.assertTrue(summary["has_unattributed_model"])

    def test_usage_summary_only_summary_has_no_steps(self):
        sessions_steps = [(make_session("s-1", model="model-a",
                                        usage={"input": 42}), [])]
        summary = hr.build_usage_summary("run-x", sessions_steps, only_summary=True)

        self.assertEqual(summary["steps"], 0)
        self.assertEqual(summary["by_model"]["model-a"]["steps"], 0)
        self.assertEqual(summary["total"]["input"], 42)
        self.assertIn("--only-summary", summary["note"])

    def test_norm_usage_junk_values(self):
        self.assertEqual(hr.norm_usage(None),
                         {"cache_read": 0, "cache_write": 0, "input": 0,
                          "output": 0, "reasoning": 0})
        self.assertEqual(hr.norm_usage({"input": True, "output": "5",
                                        "reasoning": 2.9})["input"], 0)
        self.assertEqual(hr.norm_usage({"output": "5"})["output"], 0)
        self.assertEqual(hr.norm_usage({"reasoning": 2.9})["reasoning"], 2)


# --------------------------------------------------------------------------
# 5) 公开模式路径前缀
# --------------------------------------------------------------------------

class TestPublicMode(unittest.TestCase):

    def test_paths_private_vs_public(self):
        private = hr.HttpTransport(base="https://example.test", token="t")
        public = hr.HttpTransport(base="https://example.test", public=True)

        self.assertEqual(private.sessions_path("run 1"),
                         "/api/v1/runs/run%201/llm/sessions")
        self.assertEqual(private.sessions_path("run 1", "s-1"),
                         "/api/v1/runs/run%201/llm/sessions/s-1")
        self.assertEqual(public.sessions_path("run 1"),
                         "/api/v1/leaderboard/agent/run%201/llm/sessions")
        self.assertEqual(public.sessions_path("run 1", "s-1"),
                         "/api/v1/leaderboard/agent/run%201/llm/sessions/s-1")

    def test_public_flag_switches_request_url(self):
        """--public 走榜单前缀，且不带 Authorization 头。"""
        calls = []

        def handler(url):
            calls.append(url)
            return {"items": [make_session("s-1")],
                    "pagination": {"page": 1, "page_size": 50, "total": 1,
                                   "total_pages": 1}}

        fake = FakeUrlopen(handler)
        transport = hr.HttpTransport(base="https://example.test", public=True)
        with mock.patch.object(hr, "urlopen", fake):
            sessions, _ = hr.fetch_sessions(transport, "run-7")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(calls, [
            "https://example.test/api/v1/leaderboard/agent/run-7/llm/sessions"
            "?page=1&page_size=50"])
        self.assertNotIn("Authorization", fake.headers[0])

    def test_private_mode_sends_bearer_and_detail_params(self):
        calls = []

        def handler(url):
            calls.append(url)
            if "/llm/sessions/s-1" in url:
                return {"pagination": {"page": 1, "page_size": 50, "total": 1,
                                       "total_pages": 1},
                        "session": {"id": "s-1", "model": "model-a"},
                        "steps": []}
            return {"items": [make_session("s-1")],
                    "pagination": {"page": 1, "page_size": 50, "total": 1,
                                   "total_pages": 1}}

        fake = FakeUrlopen(handler)
        transport = hr.HttpTransport(base="https://example.test", token="tok-123")
        with mock.patch.object(hr, "urlopen", fake):
            sessions, _ = hr.fetch_sessions(transport, "run-7")
            hr.fetch_session_detail(transport, sessions[0], "run-7")

        self.assertEqual(fake.headers[0]["Authorization"], "Bearer tok-123")
        self.assertTrue(calls[1].startswith(
            "https://example.test/api/v1/runs/run-7/llm/sessions/s-1?"))
        self.assertIn("from=2025-12-31T23%3A59%3A00%2B00%3A00", calls[1])
        self.assertIn("to=2026-01-01T01%3A01%3A00%2B00%3A00", calls[1])

    def test_bearer_prefix_not_doubled(self):
        transport = hr.HttpTransport(token="Bearer abc")
        self.assertEqual(transport._headers()["Authorization"], "Bearer abc")


# --------------------------------------------------------------------------
# 6) 错误处理：重试一次 + 单会话失败不中断
# --------------------------------------------------------------------------

class TestErrorHandling(TempFixtureCase):

    def test_http_error_retried_once_then_raises_with_body_head(self):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"error":"bad token"}'))

        transport = hr.HttpTransport(base="https://example.test", token="t", retries=1)
        with mock.patch.object(hr, "urlopen", fake_urlopen), \
                mock.patch.object(hr.time, "sleep", lambda s: None):
            with self.assertRaises(hr.FetchError) as ctx:
                transport.sessions_page("run-1", 1, 50)

        self.assertEqual(len(attempts), 2)          # 首次 + 重试一次
        self.assertIn("401", str(ctx.exception))
        self.assertIn("bad token", str(ctx.exception))   # body 摘要

    def test_single_session_failure_does_not_abort_run(self):
        """一个会话详情挂掉，其余照常产出，warning 落到 sessions.json。"""
        s1 = make_session("s-ok")
        s2 = make_session("s-bad")
        steps = [make_step(1, "2026-01-01T00:00:01+00:00",
                           [make_item(text="hello world")])]

        def handler(url):
            if "/s-bad" in url:
                raise urllib.error.HTTPError(url, 500, "Server Error", {},
                                             io.BytesIO(b'{"error":"boom"}'))
            if "/llm/sessions?" in url:
                return {"items": [s1, s2],
                        "pagination": {"page": 1, "page_size": 50, "total": 2,
                                       "total_pages": 1}}
            return {"pagination": {"page": 1, "page_size": 50, "total": 1,
                                   "total_pages": 1},
                    "session": {"id": "s-ok"}, "steps": steps}

        fake = FakeUrlopen(handler)
        transport = hr.HttpTransport(base="https://example.test", token="t", retries=0)
        outdir = os.path.join(self.tmp, "replay")
        args = hr.build_parser().parse_args(
            ["--run-id", "42", "--outdir", outdir, "--base", "https://example.test",
             "--token", "t"])

        with mock.patch.object(hr, "urlopen", fake):
            rc = hr.run(args)

        self.assertEqual(rc, 0)
        for name in ("timeline.jsonl", "file-tree.json", "deliverables.md",
                     "usage-summary.json", "sessions.json"):
            self.assertTrue(os.path.isfile(os.path.join(outdir, name)), name)

        timeline = [json.loads(ln) for ln in
                    self.read_lines(os.path.join(outdir, "timeline.jsonl"))]
        self.assertEqual(len(timeline), 1)          # 只有 s-ok 的 item

        meta = self.read_json(os.path.join(outdir, "sessions.json"))
        self.assertEqual(meta["count"], 2)
        self.assertTrue(any("s-bad" in w for w in meta["warnings"]))


# --------------------------------------------------------------------------
# 7) deliverables 与端到端 dry-run
# --------------------------------------------------------------------------

class TestDeliverablesAndDryRun(TempFixtureCase):

    def test_picks_last_long_assistant_text(self):
        short = "brief"
        good = "G" * 250
        better = "B" * 300
        sessions_steps = [
            (make_session("s-1"),
             [make_step(1, "2026-01-01T00:00:01+00:00",
                        [make_item(kind="text", text=good)]),
              make_step(2, "2026-01-01T00:00:02+00:00",
                        [make_item(kind="text", text=short)]),
              make_step(3, "2026-01-01T00:00:03+00:00",
                        [make_item(kind="text", text=better)]),
              make_step(4, "2026-01-01T00:00:04+00:00",
                        [make_item(kind="reasoning", text="R" * 400)])]),
            (make_session("s-2"),
             [make_step(1, "2026-01-01T00:00:01+00:00",
                        [make_item(kind="text", text="too short")])]),
        ]

        picked = hr.build_deliverables(sessions_steps)

        self.assertEqual(len(picked), 1)             # s-2 无合格交付物 -> 跳过
        self.assertEqual(picked[0]["sid"], "s-1")
        self.assertEqual(picked[0]["text"], better)

        outdir = os.path.join(self.tmp, "out")
        hr.ensure_outdir(outdir)
        path = hr.write_deliverables(outdir, "run-x", picked, only_summary=False)
        body = self.read_text(path)
        self.assertIn("s-1", body)
        self.assertIn("model: synthetic-model-a", body)
        self.assertIn(better, body)
        self.assertNotIn("too short", body)

    def test_deliverables_skip_when_none(self):
        outdir = os.path.join(self.tmp, "out2")
        hr.ensure_outdir(outdir)
        path = hr.write_deliverables(outdir, "run-x", [], only_summary=True)
        body = self.read_text(path)
        self.assertIn("--only-summary", body)

    def _fixture_dir(self):
        fixdir = os.path.join(self.tmp, "fixture")
        os.makedirs(fixdir)
        s1 = make_session("s-1", model="model-a",
                          usage={"input": 100, "output": 50, "cache_read": None})
        s2 = make_session("s-2", model="model-b", usage=None)
        with open(os.path.join(fixdir, "sessions.json"), "w", encoding="utf-8") as fh:
            json.dump([page([s1], 1, 1, total_pages=2), page([s2], 2, 1, total_pages=2)],
                      fh, ensure_ascii=False)

        s1_steps = [
            make_step(1, "2026-01-01T00:00:01+00:00", [
                make_item(kind="text", text="D" * 220),
                make_item(kind="tool-call", name="write", role="assistant",
                          args=json.dumps({"file_path": "/home/user/work/repo/src/a.txt",
                                           "content": "hello"})),
            ], usage={"input": 10, "output": 2}),
            make_step(2, "2026-01-01T00:00:02+00:00", [
                make_item(kind="tool-call", name="read", role="assistant",
                          args={"file_path": "src/a.txt"}),
            ]),
        ]
        s2_steps = [
            make_step(1, "2026-01-01T00:20:00+00:00", [
                make_item(role="user", text="go"),
            ], usage={"input": 7}),
        ]
        for sid, steps in (("s-1", s1_steps), ("s-2", s2_steps)):
            with open(os.path.join(fixdir, "session-%s.json" % sid), "w",
                      encoding="utf-8") as fh:
                json.dump([page(steps, 1, 1, total_pages=2, key="steps"),
                           page([], 2, 1, total_pages=2, key="steps")],
                          fh, ensure_ascii=False)
        return fixdir

    def test_dry_run_end_to_end(self):
        fixdir = self._fixture_dir()
        outdir = os.path.join(self.tmp, "e2e")
        args = hr.build_parser().parse_args(
            ["--run-id", "999", "--outdir", outdir, "--dry-run", fixdir])

        rc = hr.run(args)
        self.assertEqual(rc, 0)

        # a) timeline：所有 item 一行
        rows = [json.loads(ln) for ln in
                self.read_lines(os.path.join(outdir, "timeline.jsonl"))]
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["sid"] for r in rows}, {"s-1", "s-2"})

        # b) file-tree：绝对路径按 working_directory 清洗
        tree = self.read_json(os.path.join(outdir, "file-tree.json"))
        self.assertEqual(sorted(tree["files"]), ["src/a.txt"])
        self.assertEqual(tree["files"]["src/a.txt"]["char_count"], 5)
        self.assertEqual([e["path"] for e in tree["read_events"]], ["src/a.txt"])

        # c) deliverables：s-1 的长 text
        body = self.read_text(os.path.join(outdir, "deliverables.md"))
        self.assertIn("## s-1", body)
        self.assertNotIn("## s-2", body)

        # d) usage-summary：step 层聚合 + session 层 total
        summary = self.read_json(os.path.join(outdir, "usage-summary.json"))
        self.assertEqual(summary["by_model"]["model-a"]["input"], 10)   # step 层
        self.assertEqual(summary["total"]["input"], 100)                # session 层
        self.assertEqual(summary["sessions"], 2)

    def test_dry_run_only_summary(self):
        fixdir = self._fixture_dir()
        outdir = os.path.join(self.tmp, "summary")
        args = hr.build_parser().parse_args(
            ["--run-id", "999", "--outdir", outdir, "--dry-run", fixdir,
             "--only-summary"])

        rc = hr.run(args)
        self.assertEqual(rc, 0)

        rows = self.read_lines(os.path.join(outdir, "timeline.jsonl"))
        self.assertEqual(rows, [])
        summary = self.read_json(os.path.join(outdir, "usage-summary.json"))
        self.assertEqual(summary["steps"], 0)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["total"]["input"], 100)

    def test_default_outdir_naming(self):
        args = hr.build_parser().parse_args(["--run-id", "777", "--dry-run", self.tmp])
        self.assertIsNone(args.outdir)   # run() 缺省拼 ./hosted-replay-<run_id>/
        expected = os.path.join(".", "hosted-replay-777")
        self.assertEqual(expected, os.path.join(".", "hosted-replay-%s" % args.run_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
