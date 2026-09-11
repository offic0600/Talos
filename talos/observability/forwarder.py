#!/usr/bin/env python3
"""
forwarder.py — staging → Elasticsearch（宿主机侧独立进程）

从 ``~/.hermes/trace/staging/`` 读取 JSONL trace 文件，批量发送到 ES，
成功后移到 ``~/.hermes/trace/archived/``。

设计约束（来自《详细设计第一批》§2.5 + 《第二批》§10）：
- **幂等**：用行内 ``delivery_id`` 作 ES ``_id``，重复投递不产生重复文档。
- **断点续投**：每个文件配一个 ``.offset`` 文件记录已投递字节数，中断后从断点续投。
- **与 harvest 的时序约束**：forwarder 只读 STAGING 下普通目录，无条件跳过
  ``.incoming/``。harvest 先把 ``docker cp`` 结果落到 ``.incoming/<task_id>/``，
  再逐文件 ``os.replace`` 进 ``STAGING/<task_id>/``。因此 forwarder 看到的每个
  ``.jsonl`` 都是完整搬运过的文件，不存在「读到半写文件」的窗口。
- **ES 挂了不影响 worker**：ES 不可达时数据留在 staging，下轮重试，不丢数据。
- **索引模板**：启动时 ``PUT _index_template``（``es-template.json``），信封字段
  显式映射，``raw`` 字段不索引。
- **索引名**：``talos-YYYY.MM.dd``（按日期分索引，由 ES data_stream 或手动命名）。

信封字段（第二批 §10 / 待决策清单 E1）：
  task_id, run_id, model, provider, api_request（POST body）,
  response_status, latency_ms, input_tokens, output_tokens,
  cost（留空，不做定价）, timestamp, prev_hook_ms

  ``cost`` 始终留空（``None``），待定价模块上线后填充。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────

STAGING = os.environ.get("TRACE_STAGING", os.path.expanduser("~/.hermes/trace/staging"))
ARCHIVE = os.environ.get("TRACE_ARCHIVE", os.path.expanduser("~/.hermes/trace/archived"))
INCOMING_NAME = ".incoming"  # harvest 的暂存区，forwarder 绝不进入

ES_URL = os.environ.get("TALOS_ES_URL", os.environ.get("ES_URL", "http://127.0.0.1:9200"))
ES_INDEX_PREFIX = os.environ.get("TALOS_ES_INDEX_PREFIX", "talos")
ES_TEMPLATE_FILE = os.path.join(os.path.dirname(__file__), "es-template.json")

INTERVAL = float(os.environ.get("FORWARD_INTERVAL", "10"))
BATCH = int(os.environ.get("FORWARD_BATCH", "200"))
BACKLOG_ALERT = int(os.environ.get("TRACE_BACKLOG_ALERT", "10000"))
ES_TIMEOUT = int(os.environ.get("FORWARD_ES_TIMEOUT", "30"))

# ── 信封字段（显式映射到 ES template 的字段） ──────────────────

ENVELOPE_FIELDS = (
    "task_id",
    "run_id",
    "model",
    "provider",
    "api_request",
    "response_status",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "cost",
    "timestamp",
    "prev_hook_ms",
)


# ── ES 索引模板 ────────────────────────────────────────────────

def put_index_template() -> bool:
    """启动时 PUT _index_template，确保信封字段映射 + raw 不索引。

    失败只打日志、不退出——ES 还没起来时 forwarder 先空转等待，ES 恢复后
    下一个 tick 会重试 PUT。模板已存在时 ES 返回 200，幂等。
    """
    if not os.path.isfile(ES_TEMPLATE_FILE):
        print(f"[forwarder] ⚠️ 索引模板文件不存在: {ES_TEMPLATE_FILE}", file=sys.stderr)
        return False
    with open(ES_TEMPLATE_FILE, encoding="utf-8") as f:
        body = f.read().encode()
    req = urllib.request.Request(
        f"{ES_URL}/_index_template/talos",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=ES_TIMEOUT) as resp:
            ok = resp.status < 300
            if ok:
                print(f"[forwarder] 索引模板已更新: {resp.status}", file=sys.stderr)
            return ok
    except Exception as exc:
        print(f"[forwarder] PUT _index_template 失败: {exc}", file=sys.stderr)
        return False


# ── 索引名 ─────────────────────────────────────────────────────

def _index_name(rec: dict) -> str:
    """按 timestamp 生成 talos-YYYY.MM.dd 索引名。

    优先用记录里的 ``timestamp`` 或 ``ts``；没有就用当前日期。
    """
    ts = rec.get("timestamp") or rec.get("ts")
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(tz=timezone.utc)
    else:
        dt = datetime.now(tz=timezone.utc)
    return f"{ES_INDEX_PREFIX}-{dt.strftime('%Y.%m.%d')}"


# ── 信封转换 ───────────────────────────────────────────────────

def _to_envelope(rec: dict) -> dict:
    """把原始 trace 记录转换为 ES 信封格式。

    原始记录（来自 trace-collector）的字段名与信封字段名有差异：
    - prompt_tokens → input_tokens
    - completion_tokens → output_tokens
    - api_duration_s → latency_ms（秒转毫秒）
    - ts → timestamp

    保留原始记录的完整内容在 ``raw`` 字段里（不索引）。
    """
    raw_ts = rec.get("ts")
    if isinstance(raw_ts, (int, float)):
        timestamp = raw_ts
    elif isinstance(raw_ts, str):
        try:
            timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            timestamp = time.time()
    else:
        timestamp = time.time()

    api_duration = rec.get("api_duration_s")
    latency_ms = None
    if isinstance(api_duration, (int, float)):
        latency_ms = round(api_duration * 1000, 3)

    envelope = {
        "task_id": rec.get("task_id") or "",
        "run_id": rec.get("run_id") or "",
        "model": rec.get("model") or "",
        "provider": rec.get("provider") or "",
        "api_request": rec.get("api_request") or rec.get("request_body") or "",
        "response_status": rec.get("response_status"),
        "latency_ms": latency_ms,
        "input_tokens": rec.get("prompt_tokens") or rec.get("input_tokens"),
        "output_tokens": rec.get("completion_tokens") or rec.get("output_tokens"),
        "cost": None,  # 留空，不做定价（E1）
        "timestamp": timestamp,
        "prev_hook_ms": rec.get("prev_hook_ms"),
        # 保留原始记录用于排查（不索引，es-template.json 里 enabled: false）
        "raw": rec,
        # 辅助字段（也在 template 里有映射）
        "delivery_id": rec.get("delivery_id") or "",
        "kind": rec.get("kind") or "",
        "session_id": rec.get("session_id") or "",
        "tenant": rec.get("tenant") or "",
        "board": rec.get("board") or "",
        "profile": rec.get("profile") or "",
        "ts": raw_ts,
    }
    return envelope


# ── staging 遍历 ───────────────────────────────────────────────

def _walk_staging():
    """遍历 staging，跳过 harvest 的暂存目录。"""
    for root, dirs, files in os.walk(STAGING):
        dirs[:] = [d for d in dirs if d != INCOMING_NAME]
        yield root, files


# ── ES bulk ────────────────────────────────────────────────────

def _bulk(lines: list[dict]) -> bool:
    """ES _bulk。用 delivery_id 作 _id，天然幂等。"""
    body = []
    for rec in lines:
        envelope = _to_envelope(rec)
        idx = _index_name(rec)
        did = envelope.get("delivery_id") or envelope.get("task_id") or ""
        body.append(json.dumps({"index": {"_index": idx, "_id": did}}))
        body.append(json.dumps(envelope, ensure_ascii=False))
    payload = ("\n".join(body) + "\n").encode()
    req = urllib.request.Request(
        f"{ES_URL}/_bulk",
        data=payload,
        headers={"Content-Type": "application/x-ndjson"},
    )
    with urllib.request.urlopen(req, timeout=ES_TIMEOUT) as resp:
        return resp.status < 300


# ── 断点续投 ───────────────────────────────────────────────────

def _offset_path(path: str) -> str:
    return path + ".offset"


def _read_offset(path: str) -> int:
    try:
        with open(_offset_path(path)) as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _write_offset(path: str, off: int) -> None:
    tmp = _offset_path(path) + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(off))
    os.replace(tmp, _offset_path(path))  # 原子替换，避免半写


def forward_file(path: str) -> bool:
    """从断点续投一个文件。全部投完返回 True。"""
    off = _read_offset(path)
    size = os.path.getsize(path)
    if off >= size:
        return True
    with open(path, encoding="utf-8") as f:
        f.seek(off)
        batch: list[dict] = []
        new_off = off
        for line in f:
            new_off += len(line.encode())
            line = line.strip()
            if not line:
                continue
            try:
                batch.append(json.loads(line))
            except Exception:
                continue  # 坏行跳过，不阻塞整个文件
            if len(batch) >= BATCH:
                if not _bulk(batch):
                    return False
                _write_offset(path, new_off)
                batch = []
        if batch:
            if not _bulk(batch):
                return False
            _write_offset(path, new_off)
    return True


# ── 积压告警 ───────────────────────────────────────────────────

def _backlog_bytes() -> int:
    total = 0
    for root, files in _walk_staging():
        for fn in files:
            if fn.endswith(".jsonl"):
                p = os.path.join(root, fn)
                total += os.path.getsize(p) - _read_offset(p)
    return total


# ── tick ───────────────────────────────────────────────────────

def tick() -> dict:
    """单次扫描：转发所有 staging 下的 .jsonl，成功后移到 archived。"""
    os.makedirs(STAGING, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    done = failed = 0
    for root, files in _walk_staging():
        for fn in sorted(files):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(root, fn)
            try:
                ok = forward_file(path)
            except (urllib.error.URLError, OSError) as exc:
                # ES 不可达 / 慢：留在 staging，下轮重试。不丢数据，也不阻塞任何人。
                print(f"[forwarder] ES 暂不可用，保留待投: {exc}", file=sys.stderr)
                return {"done": done, "failed": failed + 1, "es_down": True}
            if ok:
                rel = os.path.relpath(path, STAGING)
                dest = os.path.join(ARCHIVE, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(path, dest)
                if os.path.exists(_offset_path(path)):
                    os.remove(_offset_path(path))
                done += 1
            else:
                failed += 1

    backlog = _backlog_bytes()
    if backlog > BACKLOG_ALERT:
        print(
            f"[forwarder] ⚠️ 待投积压 {backlog} 字节，超过阈值 {BACKLOG_ALERT}",
            file=sys.stderr,
        )
    return {"done": done, "failed": failed, "backlog_bytes": backlog}


# ── 主循环 ─────────────────────────────────────────────────────

def main() -> None:
    template_ok = put_index_template()
    if not template_ok:
        print("[forwarder] 索引模板未就绪，将在后续 tick 中重试", file=sys.stderr)

    if "--once" in sys.argv:
        # 单次模式（测试用）
        if not template_ok:
            put_index_template()
        print(json.dumps(tick(), ensure_ascii=False))
        return

    template_done = template_ok
    while True:
        try:
            if not template_done:
                template_done = put_index_template()
            print(json.dumps(tick(), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(f"[forwarder] tick 异常: {exc}", file=sys.stderr)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
