#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hosted-replay —— 托管局赛后会话回放重建器（金柝 / 神策 P2）

把 tsecbench 平台托管模式跑完后的全量 LLM 会话（平台保留 24 个月）拉回本地，
重建为可诊断的产物：时间线、按 write/edit 工具调用重建的文件树、交付物摘要、模型用量汇总。

用法
----
    # 认证：--token 或环境变量 TSEC_SESSION
    export TSEC_SESSION=<platform-session-token>
    python3 hosted-replay.py --run-id 12345
    python3 hosted-replay.py --run-id 12345 --outdir ./replay-12345
    python3 hosted-replay.py --run-id 12345 --only-summary      # 只拉列表层，快速出 usage-summary
    python3 hosted-replay.py --run-id 12345 --public            # 公开榜单前缀，无需认证
    python3 hosted-replay.py --run-id 12345 --dry-run ./fixture # 离线回放本地 JSON（自测用）

产物（全部写 --outdir，默认 ./hosted-replay-<run_id>/）
--------------------------------------------------------
  a) timeline.jsonl     每条 item 一行
  b) file-tree.json     {files: {...}, read_events: [...], write_events: [...]}
  c) deliverables.md    每 session 最后一条 role=assistant & kind=text & char_len>=200 的 text
  d) usage-summary.json 按 model 聚合（只累计 step 层，session.usage 单独放 total）
  +) sessions.json       会话列表原始元数据 + 本次运行的 warnings（额外诊断产物）

契约来源（本任务描述的实测 schema；全部字段按“可选”处理，缺失不崩）
--------------------------------------------------------------------
  BASE = https://tsecbench.zc.tencent.com
  认证：HTTP 头 `Authorization: Bearer <token>`（--token / env TSEC_SESSION）

  1) GET {BASE}/api/v1/runs/{run_id}/llm/sessions?page=N&page_size=50
     -> {"items":[session...], "pagination":{page,page_size,total,total_pages}}
     session 字段（全部可选）：id, group_id, task_code, model, protocol, status,
       closed_reason, first_captured_at, last_active_at, event_count, range_call_count,
       explicit_id, title, working_directory, usage{cache_read,cache_write,input,output,reasoning}

  2) GET {BASE}/api/v1/runs/{run_id}/llm/sessions/{sid}?from=<ISO>&to=<ISO>
     （from/to 必填，取 first_captured_at/last_active_at 前后各留 60s 余量）
     -> {"pagination":{...}, "session":{...}, "steps":[step...]}
     step: id, seq, captured_at, event_type, state, stop_reason, render_state, error,
           raw_exchange_id, usage{...}, items[item...]
     item: role, kind, name, call_id, args(null|JSON字符串|dict), text, char_len, fp, is_error
     kind 取值（不限于）：text / reasoning / tool-call / tool-result / system_note
     role 取值（不限于）：assistant / user / system / tool
     详情同样分页（按 steps 翻页，响应带 pagination.total_pages）

  3) 公开模式（--public）：GET {BASE}/api/v1/leaderboard/agent/{run_id}/llm/sessions
     （前缀不同，无需认证，其余相同）

防御式解析约定
--------------
  * 数值字段为 null / 非数值 / 缺失 -> 按 0 计
  * args 可能是 JSON 字符串、已解析 dict，或 null -> 三种都处理
  * 路径字段兼容 file_path / path / filePath 三种命名；内容字段兼容 content / text
  * timeline 的 text_head = item.text（为空时回退到 args 的 JSON 文本）前 200 字符
  * timeline 的 char_len = item.char_len（缺失时回退到 len(item.text)）
  * step_usage / usage-summary 中的 usage 一律归一化为 5 个整数字段
  * 单会话拉取失败不中断全量：warning 到 stderr，继续下一个；HTTP 请求重试一次
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DEFAULT = "https://tsecbench.zc.tencent.com"
ENV_TOKEN = "TSEC_SESSION"
PAGE_SIZE_DEFAULT = 50
MAX_PAGES = 1000            # 死循环兜底
TIMEOUT = 30
RETRIES = 1                 # 失败后额外重试次数（共 2 次尝试）
USAGE_KEYS = ("cache_read", "cache_write", "input", "output", "reasoning")
PATH_KEYS = ("file_path", "path", "filePath")
CONTENT_KEYS = ("content", "text")
WRITE_TOOL_NAMES = ("write", "edit")
DELIVERABLE_MIN_CHARS = 200
TEXT_HEAD_LEN = 200

# 模块级间接层：测试可替换为假实现
urlopen = urllib.request.urlopen


def warn(msg):
    sys.stderr.write("[hosted-replay] WARN: %s\n" % msg)
    sys.stderr.flush()


def info(msg):
    sys.stderr.write("[hosted-replay] %s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# 归一化 / 解析工具
# --------------------------------------------------------------------------

def as_dict(value):
    """把可能是 dict / JSON 字符串 / None 的值统一成 dict（失败返回 {}）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def norm_usage(usage):
    """usage -> 5 个整数字段；null / 非数值 / 缺失按 0 计。"""
    out = {}
    src = usage if isinstance(usage, dict) else {}
    for key in USAGE_KEYS:
        val = src.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            out[key] = 0
        else:
            out[key] = int(val)
    return out


def add_usage(acc, usage):
    for key in USAGE_KEYS:
        acc[key] = acc.get(key, 0) + int(usage.get(key, 0) or 0)
    return acc


def empty_usage():
    return dict((key, 0) for key in USAGE_KEYS)


def parse_iso(value):
    """宽容解析 ISO 时间；失败返回 None。"""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def iso_shift(value, seconds):
    """把 ISO 时间平移 seconds；不可解析时返回 None。"""
    dt = parse_iso(value)
    if dt is None:
        return None
    try:
        return (dt + datetime.timedelta(seconds=seconds)).isoformat()
    except (OverflowError, ValueError):
        return None


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def clean_path(raw, working_directory=None):
    """把工具调用里的路径清洗成仓库内相对路径。

    - 反斜杠统一为 /，去掉 Windows 盘符
    - 去掉 working_directory 前缀（若给出）
    - 解析 . / .. 段，丢弃逃出仓库的前导 ..
    - 去掉前导 /（绝对前缀）
    返回清洗后的相对路径；无法得到有效相对路径时返回 None。
    """
    if not isinstance(raw, str):
        return None
    p = raw.strip().strip('"').strip("'").strip()
    if not p:
        return None
    p = p.replace("\\", "/")
    p = _WIN_DRIVE_RE.sub("", p, count=1)

    if isinstance(working_directory, str) and working_directory.strip():
        wd = working_directory.strip().replace("\\", "/")
        wd = _WIN_DRIVE_RE.sub("", wd, count=1)
        wd = wd.rstrip("/")
        if wd:
            if p == wd:
                return None
            if p.startswith(wd + "/"):
                p = p[len(wd) + 1:]

    parts = []
    for seg in p.split("/"):
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue  # 逃出仓库的前导 .. 直接丢弃
        parts.append(seg)

    cleaned = "/".join(parts)
    if not cleaned or cleaned in (".", ".."):
        return None
    return cleaned


def arg_path(args, working_directory=None):
    """从（可能非 dict 的）args 中取路径字段并清洗。"""
    data = as_dict(args)
    for key in PATH_KEYS:
        if key in data:
            cleaned = clean_path(data.get(key), working_directory)
            if cleaned:
                return cleaned
    return None


def arg_content(args):
    """从 args 中取内容字段；返回 (内容, 是否为完整内容)。"""
    data = as_dict(args)
    for key in CONTENT_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val:
            return val, True
    return "", False


def item_text(item):
    """item 的文本（tool-call 无 text 时回退到 args 的 JSON 文本）。"""
    text = item.get("text")
    if isinstance(text, str) and text:
        return text
    args = item.get("args")
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


def item_char_len(item):
    """item 的字符数：优先平台 char_len，缺失时回退 len(text)。"""
    val = item.get("char_len")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        text = item.get("text")
        return len(text) if isinstance(text, str) else 0
    return int(val)


def step_items(step):
    items = step.get("items")
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


class Transport(object):
    """会话数据源接口。返回原始响应 dict。"""

    kind = "base"
    description = ""

    def sessions_page(self, run_id, page, page_size):
        raise NotImplementedError

    def session_page(self, run_id, sid, frm, to, page, page_size):
        raise NotImplementedError

    def close(self):
        pass


class HttpTransport(Transport):
    kind = "http"

    def __init__(self, base=BASE_DEFAULT, token=None, public=False,
                 timeout=TIMEOUT, retries=RETRIES):
        self.base = (base or BASE_DEFAULT).rstrip("/")
        self.token = token
        self.public = bool(public)
        self.timeout = timeout
        self.retries = max(0, int(retries))

    # ---- URL 构造（公开模式只换前缀，其余相同）----
    def sessions_path(self, run_id, sid=None):
        rid = urllib.parse.quote(str(run_id), safe="")
        if self.public:
            path = "/api/v1/leaderboard/agent/%s/llm/sessions" % rid
        else:
            path = "/api/v1/runs/%s/llm/sessions" % rid
        if sid is not None:
            path += "/" + urllib.parse.quote(str(sid), safe="")
        return path

    def build_url(self, path, params=None):
        url = self.base + path
        clean = {}
        for key, val in (params or {}).items():
            if val is None:
                continue
            clean[key] = val
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        return url

    def _headers(self):
        headers = {"Accept": "application/json", "User-Agent": "hosted-replay/1.0"}
        if self.token:
            tok = self.token.strip()
            headers["Authorization"] = tok if tok.lower().startswith("bearer ") \
                else "Bearer " + tok
        return headers

    def get_json(self, path, params=None):
        url = self.build_url(path, params)
        last_err = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(1.0)
                warn("retrying (%d/%d): %s" % (attempt, self.retries, url))
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                try:
                    return json.loads(body.decode("utf-8", "replace"))
                except ValueError as exc:
                    raise FetchError("non-JSON response from %s: %s (body head: %s)"
                                     % (url, exc, _head(body)))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = _head(exc.read())
                except Exception:
                    detail = "<unreadable>"
                last_err = FetchError("HTTP %s for %s | body head: %s"
                                      % (exc.code, url, detail))
            except urllib.error.URLError as exc:
                last_err = FetchError("network error for %s: %s" % (url, exc.reason))
            except FetchError as exc:
                last_err = exc
        raise last_err if last_err else FetchError("request failed: %s" % url)

    def sessions_page(self, run_id, page, page_size):
        return self.get_json(self.sessions_path(run_id),
                             {"page": page, "page_size": page_size})

    def session_page(self, run_id, sid, frm, to, page, page_size):
        return self.get_json(self.sessions_path(run_id, sid),
                             {"from": frm, "to": to, "page": page,
                              "page_size": page_size})


class FixtureTransport(Transport):
    """离线回放：从目录读本地 JSON，供 --dry-run 与单元测试使用。

    目录内文件：
      sessions.json           单个响应 dict，或按页顺序排列的响应 list
      session-<sid>.json      同上（会话详情分页）
    """

    kind = "fixture"

    def __init__(self, directory):
        self.dir = directory
        self.description = "fixture:%s" % directory

    def _page_at(self, filename, page, key):
        path = os.path.join(self.dir, filename)
        if not os.path.isfile(path):
            raise FetchError("fixture not found: %s" % path)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            if page - 1 < len(data):
                return data[page - 1]
            return {key: [], "pagination": {"page": page, "page_size": len(data) or 1,
                                            "total": 0, "total_pages": len(data)}}
        if isinstance(data, dict):
            if page == 1:
                return data
            return {key: [], "pagination": {"page": page, "page_size": 1,
                                            "total": 0, "total_pages": 1}}
        raise FetchError("fixture %s has unsupported shape: %s" % (path, type(data).__name__))

    def sessions_page(self, run_id, page, page_size):
        return self._page_at("sessions.json", page, "items")

    def session_page(self, run_id, sid, frm, to, page, page_size):
        return self._page_at("session-%s.json" % sid, page, "steps")


def _head(body, limit=300):
    if body is None:
        return "<empty>"
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = str(body)
    text = text.replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


# --------------------------------------------------------------------------
# 分页收集
# --------------------------------------------------------------------------

def collect_pages(fetch_page, key, page_size=PAGE_SIZE_DEFAULT, max_pages=MAX_PAGES):
    """翻完所有分页。fetch_page(page, page_size) -> 响应 dict。

    返回 (items, raw_responses)。分页元数据缺失时用“本页不足 page_size”判定末页。
    """
    items = []
    raws = []
    page = 1
    while page <= max_pages:
        resp = fetch_page(page, page_size)
        raws.append(resp)
        if not isinstance(resp, dict):
            warn("page %d: unexpected response type %s, stopping"
                 % (page, type(resp).__name__))
            break
        chunk = resp.get(key)
        if not isinstance(chunk, list):
            chunk = []
        items.extend([x for x in chunk if isinstance(x, dict)])

        pagination = resp.get("pagination")
        total_pages = None
        if isinstance(pagination, dict):
            tp = pagination.get("total_pages")
            if isinstance(tp, int) and not isinstance(tp, bool) and tp > 0:
                total_pages = tp
        if total_pages is not None:
            if page >= total_pages:
                break
        elif not chunk or len(chunk) < page_size:
            break
        if not chunk:
            warn("page %d empty while pagination claims more pages, stopping" % page)
            break
        page += 1
    else:
        warn("hit MAX_PAGES=%d guard, stopping pagination" % max_pages)
    return items, raws


def fetch_sessions(transport, run_id, page_size=PAGE_SIZE_DEFAULT):
    return collect_pages(lambda p, ps: transport.sessions_page(run_id, p, ps),
                         "items", page_size)


def fetch_session_detail(transport, transport_session, run_id, page_size=PAGE_SIZE_DEFAULT):
    """拉取单会话全部 steps，并返回 (steps, detail_session, warnings)。"""
    sid = transport_session.get("id")
    frm = iso_shift(transport_session.get("first_captured_at"), -60)
    to = iso_shift(transport_session.get("last_active_at"), 60)
    msgs = []
    if frm is None and transport_session.get("first_captured_at"):
        msgs.append("session %s: unparseable first_captured_at=%r, from omitted"
                    % (sid, transport_session.get("first_captured_at")))
    if to is None and transport_session.get("last_active_at"):
        msgs.append("session %s: unparseable last_active_at=%r, to omitted"
                    % (sid, transport_session.get("last_active_at")))

    steps, raws = collect_pages(
        lambda p, ps: transport_session_page(transport, run_id, sid, frm, to, p, ps),
        "steps", page_size)

    detail_session = {}
    for resp in raws:
        cand = resp.get("session")
        if isinstance(cand, dict):
            detail_session.update(cand)
    return steps, detail_session, msgs


def transport_session_page(transport, run_id, sid, frm, to, page, page_size):
    return transport.session_page(run_id, sid, frm, to, page, page_size)


# --------------------------------------------------------------------------
# 产物构建
# --------------------------------------------------------------------------

def build_timeline(sessions_steps):
    """sessions_steps: [(session, steps)] -> list[dict]（每条 item 一行）。"""
    rows = []
    for session, steps in sessions_steps:
        sid = session.get("id")
        for step in steps:
            step_usage = norm_usage(step.get("usage") if isinstance(step, dict) else None)
            for item in step_items(step):
                rows.append({
                    "sid": sid,
                    "step_seq": step.get("seq"),
                    "captured_at": step.get("captured_at"),
                    "event_type": step.get("event_type"),
                    "role": item.get("role"),
                    "kind": item.get("kind"),
                    "name": item.get("name"),
                    "char_len": item_char_len(item),
                    "text_head": item_text(item)[:TEXT_HEAD_LEN],
                    "is_error": bool(item.get("is_error")),
                    "step_usage": step_usage,
                })
    return rows


def build_file_tree(sessions_steps):
    """按 write/edit/read 工具调用重建文件树。"""
    files = {}
    read_events = []
    write_events = []

    for session, steps in sessions_steps:
        sid = session.get("id")
        wd = session.get("working_directory")
        for step in steps:
            captured_at = step.get("captured_at")
            seq = step.get("seq")
            for item in step_items(step):
                if item.get("kind") != "tool-call":
                    continue
                name = item.get("name")
                if name not in WRITE_TOOL_NAMES and name != "read":
                    continue
                path = arg_path(item.get("args"), wd)
                if not path:
                    continue

                if name == "read":
                    read_events.append({"path": path, "captured_at": captured_at, "sid": sid})
                    continue

                content, complete = arg_content(item.get("args"))
                if name == "edit" and not complete:
                    # edit 无完整内容 -> 只记事件，不写入 files（避免落半截内容）
                    write_events.append({"path": path, "captured_at": captured_at,
                                         "sid": sid, "step_seq": seq, "tool": name,
                                         "char_count": None, "complete": False})
                    continue

                entry = files.get(path)
                prev_at = entry.get("last_captured_at") if entry else None
                if entry is None or _iso_ge(captured_at, prev_at):
                    files[path] = {
                        "last_captured_at": captured_at,
                        "char_count": len(content),
                        "source_sid": sid,
                        "source_step_seq": seq,
                    }
                write_events.append({"path": path, "captured_at": captured_at,
                                     "sid": sid, "step_seq": seq, "tool": name,
                                     "char_count": len(content), "complete": complete})

    files = dict(sorted(files.items(), key=lambda kv: kv[0]))
    read_events.sort(key=lambda e: (e.get("captured_at") or "", str(e.get("path") or "")))
    write_events.sort(key=lambda e: (e.get("captured_at") or "", str(e.get("path") or "")))
    return {"files": files, "read_events": read_events, "write_events": write_events}


def _iso_ge(a, b):
    """ISO 时间字符串比较（b 为空视为最小）。格式不一致时退化为字符串比较。"""
    if b is None:
        return True
    da, db = parse_iso(a), parse_iso(b)
    if da is not None and db is not None:
        try:
            return da >= db
        except TypeError:  # naive vs aware
            return str(a) >= str(b)
    return str(a or "") >= str(b or "")


def build_deliverables(sessions_steps):
    """每 session 取最后一条 assistant+text 且 char_len>=200 的 item。"""
    picked = []
    for session, steps in sessions_steps:
        last = None
        for step in steps:
            for item in step_items(step):
                if item.get("role") != "assistant" or item.get("kind") != "text":
                    continue
                if item_char_len(item) < DELIVERABLE_MIN_CHARS:
                    continue
                last = (step, item)
        if last is None:
            continue
        step, item = last
        picked.append({
            "sid": session.get("id"),
            "model": session.get("model"),
            "captured_at": step.get("captured_at"),
            "seq": step.get("seq"),
            "text": item.get("text") if isinstance(item.get("text"), str) else item_text(item),
            "char_len": item_char_len(item),
        })
    picked.sort(key=lambda d: (d.get("captured_at") is None, d.get("captured_at") or ""))
    return picked


def build_usage_summary(run_id, sessions_steps, only_summary=False):
    """按 model 聚合 step 层 usage；session.usage 单独放 total（仅作参考）。

    sessions_steps: [(session, steps)]，一个会话一项（含详情拉取失败的会话）。
    """
    by_model = {}
    total_session = empty_usage()
    total_steps = 0
    has_unattributed = False
    session_count = 0

    for session, steps in sessions_steps:
        session_count += 1
        model = session.get("model")
        model = model if isinstance(model, str) and model.strip() else None
        if model is None:
            has_unattributed = True
        key = model or "unattributed"

        bucket = by_model.get(key)
        if bucket is None:
            bucket = empty_usage()
            bucket["sessions"] = 0
            bucket["steps"] = 0
            by_model[key] = bucket
        bucket["sessions"] += 1

        add_usage(total_session, norm_usage(session.get("usage")))

        for step in steps:
            if not isinstance(step, dict):
                continue
            bucket["steps"] += 1
            total_steps += 1
            add_usage(bucket, norm_usage(step.get("usage")))

    by_model = dict(sorted(by_model.items(), key=lambda kv: kv[0]))
    return {
        "run_id": run_id,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "note": ("by_model aggregates step-level usage only (no double counting); "
                 "total is session-level usage, reference only"
                 + ("; details not fetched (--only-summary)" if only_summary else "")),
        "sessions": session_count,
        "steps": total_steps,
        "by_model": by_model,
        "has_unattributed_model": has_unattributed,
        "total": total_session,
    }


# --------------------------------------------------------------------------
# 写出
# --------------------------------------------------------------------------

def ensure_outdir(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def write_timeline(outdir, rows):
    path = os.path.join(outdir, "timeline.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def write_json(outdir, filename, payload):
    path = os.path.join(outdir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def write_deliverables(outdir, run_id, deliverables, only_summary):
    path = os.path.join(outdir, "deliverables.md")
    lines = ["# Deliverables — run %s" % run_id, ""]
    if not deliverables:
        if only_summary:
            lines.append("_本轮为 --only-summary，未拉取会话详情，无交付物文本。_")
        else:
            lines.append("_未找到 role=assistant & kind=text & char_len>=%d 的交付物文本。_"
                         % DELIVERABLE_MIN_CHARS)
        lines.append("")
    for item in deliverables:
        lines.append("## %s (model: %s, captured_at: %s)"
                     % (item.get("sid"), item.get("model"),
                        item.get("captured_at")))
        lines.append("")
        lines.append("<!-- seq=%s char_len=%s -->" % (item.get("seq"), item.get("char_len")))
        lines.append("")
        lines.append(item.get("text") or "")
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(args):
    run_id = str(args.run_id)
    outdir = args.outdir or os.path.join(".", "hosted-replay-%s" % run_id)
    ensure_outdir(outdir)

    if args.dry_run:
        transport = FixtureTransport(args.dry_run)
    else:
        token = args.token or os.environ.get(ENV_TOKEN) or ""
        if not token and not args.public:
            warn("no token provided (--token or env %s); request will likely fail"
                 % ENV_TOKEN)
        transport = HttpTransport(base=args.base, token=token, public=args.public,
                                  timeout=args.timeout)

    warnings = []
    info("run_id=%s outdir=%s source=%s public=%s only_summary=%s"
         % (run_id, outdir, transport.kind, bool(args.public), bool(args.only_summary)))

    sessions, _raws = fetch_sessions(transport, run_id, args.page_size)
    info("sessions fetched: %d" % len(sessions))

    sessions_steps = []
    if args.only_summary:
        sessions_steps = [(s, []) for s in sessions]
    else:
        for idx, session in enumerate(sessions, 1):
            sid = session.get("id")
            if sid is None:
                msg = "session #%d has no id, skipped" % idx
                warn(msg)
                warnings.append(msg)
                sessions_steps.append((session, []))
                continue
            try:
                steps, detail, msgs = fetch_session_detail(transport, session, run_id,
                                                           args.page_size)
                for m in msgs:
                    warn(m)
                    warnings.append(m)
                if detail:
                    merged = dict(session)
                    merged.update(detail)
                    session = merged
                sessions_steps.append((session, steps))
                if idx % 10 == 0 or idx == len(sessions):
                    info("session details: %d/%d" % (idx, len(sessions)))
            except (FetchError, OSError, ValueError) as exc:
                msg = "session %s fetch failed: %s" % (sid, exc)
                warn(msg)
                warnings.append(msg)
                sessions_steps.append((session, []))
            except Exception as exc:  # 防御：任何单会话异常不中断全量
                msg = "session %s unexpected error: %r" % (sid, exc)
                warn(msg)
                warnings.append(msg)
                sessions_steps.append((session, []))

    timeline = build_timeline(sessions_steps)
    tree = build_file_tree(sessions_steps)
    deliverables = build_deliverables(sessions_steps)
    summary = build_usage_summary(run_id, sessions_steps,
                                  only_summary=bool(args.only_summary))

    write_timeline(outdir, timeline)
    write_json(outdir, "file-tree.json", tree)
    write_deliverables(outdir, run_id, deliverables, bool(args.only_summary))
    write_json(outdir, "usage-summary.json", summary)
    write_json(outdir, "sessions.json", {
        "run_id": run_id,
        "source": transport.kind,
        "public": bool(args.public),
        "only_summary": bool(args.only_summary),
        "count": len(sessions),
        "sessions": sessions,
        "warnings": warnings,
    })

    info("done: timeline=%d rows, files=%d, read_events=%d, deliverables=%d"
         % (len(timeline), len(tree["files"]), len(tree["read_events"]),
            len(deliverables)))
    info("artifacts in %s" % os.path.abspath(outdir))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hosted-replay.py",
        description="托管局赛后会话回放重建器：把 tsecbench 平台托管会话重建为本地诊断产物",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="认证：--token 或环境变量 %s（Authorization: Bearer <token>）" % ENV_TOKEN)
    parser.add_argument("--run-id", required=True, help="托管局 run id（必填）")
    parser.add_argument("--token", default=None,
                        help="平台会话 token；缺省读环境变量 %s" % ENV_TOKEN)
    parser.add_argument("--public", action="store_true",
                        help="使用公开榜单前缀 /api/v1/leaderboard/agent/...（无需认证）")
    parser.add_argument("--base", default=BASE_DEFAULT, help="平台 BASE，默认 %s" % BASE_DEFAULT)
    parser.add_argument("--outdir", default=None,
                        help="产物目录，默认 ./hosted-replay-<run_id>/")
    parser.add_argument("--only-summary", action="store_true",
                        help="只拉 sessions 列表层，不拉每会话详情（快速出 usage-summary）")
    parser.add_argument("--dry-run", default=None, metavar="DIR",
                        help="离线回放：从 DIR 读 sessions.json / session-<sid>.json，不发网络请求")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE_DEFAULT,
                        help="分页大小，默认 %d" % PAGE_SIZE_DEFAULT)
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="HTTP 超时秒数")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        warn("interrupted")
        return 130
    except FetchError as exc:
        warn("fatal: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
