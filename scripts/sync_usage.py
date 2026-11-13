#!/usr/bin/env python3
"""Sync Antigravity native model & subagent usage from local SQLite trajectories to Magpie."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

if sys.version_info < (3, 8):
    raise SystemExit("magpie-usage requires Python 3.8 or newer.")

DEFAULT_MAGPIE_URL = "http://127.0.0.1:3425/v1/magpie/usage/requests"
BATCH_SIZE = 500

# Fallback map for older rows where gen_metadata omitted canonical model slug (field 1.19)
ENUM_FALLBACK: dict[str, tuple[str, str]] = {
    "MODEL_PLACEHOLDER_M318": ("gemini-3.8-flash", "high"),
    "MODEL_PLACEHOLDER_M319": ("gemini-3.8-flash", "medium"),
    "MODEL_PLACEHOLDER_M322": ("gemini-3.8-flash", ""),
    "MODEL_PLACEHOLDER_M417": ("gemini-4-argon", "high"),
    "MODEL_PLACEHOLDER_M418": ("gemini-4-argon", "medium"),
    "MODEL_PLACEHOLDER_M427": ("gemini-4-carbon", "high"),
    "MODEL_PLACEHOLDER_M437": ("gemini-4-carbon", "medium"),
    "MODEL_PLACEHOLDER_M525": ("gemini-4-hydrogen", "high"),
}

VARIANT_ALIASES: dict[str, str] = {
    "gemini-4-argon-d": "gemini-4-argon",
    "gemini-3.8-flash-n": "gemini-3.8-flash",
    "gemini-3.8-flash-tiered": "gemini-3.8-flash",
}

EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$", re.IGNORECASE)
DISPLAY_EFFORT_RE = re.compile(r"\((low|medium|high|xhigh|max)\)\s*$", re.IGNORECASE)


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            reconf(encoding="utf-8", errors="replace")


def _default_state_path() -> Path:
    return Path.home() / ".gemini" / "antigravity" / "plugin_data" / "magpie-usage" / "state.json"


def _resolve_caller_keys_path(home: Path) -> Path:
    candidates: list[Path] = []
    env_cfg = os.environ.get("MAGPIE_CONFIG_DIR", "").strip()
    if env_cfg:
        candidates.append(Path(env_cfg).expanduser() / "caller-keys.json")
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_cfg:
        candidates.append(Path(xdg_cfg).expanduser() / "magpie" / "caller-keys.json")
    default_cfg = home / ".config" / "magpie" / "caller-keys.json"
    candidates.append(default_cfg)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "magpie" / "caller-keys.json")

    for cand in candidates:
        if cand.is_file():
            return cand
    return candidates[0] if env_cfg else default_cfg


def _paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    env_conv = os.environ.get("ANTIGRAVITY_CONVERSATIONS_DIR", "").strip()
    conv_dir = (
        Path(env_conv).expanduser()
        if env_conv
        else home / ".gemini" / "antigravity" / "conversations"
    )
    plugin_data = os.environ.get("PLUGIN_DATA", "").strip()
    state_path = (
        Path(plugin_data).expanduser() / "state.json"
        if plugin_data
        else _default_state_path()
    )
    caller_keys = _resolve_caller_keys_path(home)
    return conv_dir, state_path, caller_keys


def parse_varint(buf: bytes | memoryview, i: int) -> tuple[int, int]:
    val = 0
    shift = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
    raise ValueError("truncated varint")


def iter_proto_fields(buf: bytes | memoryview):
    i = 0
    n = len(buf)
    mv = memoryview(buf)
    while i < n:
        try:
            tag, i = parse_varint(mv, i)
        except ValueError:
            break
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            try:
                v, i = parse_varint(mv, i)
            except ValueError:
                break
            yield fn, wt, v
        elif wt == 2:
            try:
                ln, i = parse_varint(mv, i)
            except ValueError:
                break
            if i + ln > n:
                break
            yield fn, wt, mv[i : i + ln]
            i += ln
        elif wt == 1:
            if i + 8 > n:
                break
            yield fn, wt, mv[i : i + 8]
            i += 8
        elif wt == 5:
            if i + 4 > n:
                break
            yield fn, wt, mv[i : i + 4]
            i += 4
        else:
            break


def parse_duration_ms(buf: memoryview) -> int:
    sec = 0
    nanos = 0
    for fn, wt, val in iter_proto_fields(buf):
        if wt == 0:
            if fn == 1:
                sec = val
            elif fn == 2:
                nanos = val
    return int(sec * 1000 + round(nanos / 1_000_000.0))


def parse_timestamp_ns(buf: memoryview) -> int:
    sec = 0
    nanos = 0
    for fn, wt, val in iter_proto_fields(buf):
        if wt == 0:
            if fn == 1:
                sec = val
            elif fn == 2:
                nanos = val
    if sec <= 0:
        return 0
    return sec * 1_000_000_000 + nanos


def ns_to_rfc3339(ts_ns: int) -> str:
    if ts_ns <= 0:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    sec, rem_ns = divmod(ts_ns, 1_000_000_000)
    base = dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    us = rem_ns // 1000
    return f"{base}.{us:06d}Z" if us else f"{base}Z"


def is_gateway_proxied(model_slug: str, display_name: str) -> bool:
    if display_name.startswith("[Magpie]"):
        return True
    if "/" in display_name or "/" in model_slug:
        return True
    if model_slug.startswith("models/magpie-"):
        return True
    return False


def normalize_model_and_effort(
    m19: str,
    m21: str,
    req_slug: str,
    model_enum: str,
    fallback_hook_model: str = "",
) -> tuple[str, str]:
    effort = ""
    m_disp = DISPLAY_EFFORT_RE.search(m21 or "")
    if m_disp:
        effort = m_disp.group(1).lower()
    elif req_slug:
        m_req = EFFORT_SUFFIX_RE.search(req_slug)
        if m_req:
            effort = m_req.group(1).lower()

    model = (m19 or "").strip().lower()
    if model:
        m_eff = EFFORT_SUFFIX_RE.search(model)
        if m_eff and (model.startswith("claude-") or model.startswith("gemini-")):
            if not effort:
                effort = m_eff.group(1).lower()
            model = EFFORT_SUFFIX_RE.sub("", model)
        model = VARIANT_ALIASES.get(model, model)

    if not model and req_slug:
        cand = EFFORT_SUFFIX_RE.sub("", req_slug.strip().lower())
        model = VARIANT_ALIASES.get(cand, cand)

    if not model and model_enum in ENUM_FALLBACK:
        model, enum_eff = ENUM_FALLBACK[model_enum]
        if not effort:
            effort = enum_eff

    for text_src in (m21, fallback_hook_model):
        if model or not text_src or text_src == "auto":
            continue
        if not effort:
            md = DISPLAY_EFFORT_RE.search(text_src)
            if md:
                effort = md.group(1).lower()
        clean = re.sub(r"\s*\([^)]*\)\s*$", "", text_src).strip().lower()
        slug = re.sub(r"[^a-z0-9.]+", "-", clean).strip("-")
        if "claude-opus-5.5" in slug:
            slug = "claude-opus-5-5"
        elif "claude-sonnet-5.5" in slug:
            slug = "claude-sonnet-5-5"
        elif "gpt-oss-120b" in slug:
            slug = "gpt-oss-120b-medium"
        if slug:
            model = VARIANT_ALIASES.get(slug, slug)

    if not effort and model_enum in ENUM_FALLBACK:
        effort = ENUM_FALLBACK[model_enum][1]

    return model or "antigravity-unknown", effort


def extract_step_details(
    conn: sqlite3.Connection,
    candidates: list[int],
    expected_msg_id: str = "",
) -> tuple[int, int, int, bool, bool]:
    """Return (start_ns, end_ns, think_dur_ms, has_response, has_thinking) from steps."""
    seen: set[int] = set()
    ordered = [s for s in candidates if s >= 0 and not (s in seen or seen.add(s))]
    if not ordered:
        return 0, 0, 0, False, False

    best_start = 0
    best_end = 0
    for st in ordered:
        row = conn.execute(
            "SELECT step_type, metadata, step_payload FROM steps WHERE idx = ?",
            (st,),
        ).fetchone()
        if not row or not row[1]:
            continue
        step_type, meta_blob, payload_blob = row[0], row[1], row[2]
        t_create = 0
        t_done = 0
        step_msg_id = ""
        for fn, wt, val in iter_proto_fields(meta_blob):
            if wt == 2:
                if len(val) <= 24:
                    if fn == 1:
                        t_create = parse_timestamp_ns(val)
                    elif fn == 7:
                        t_done = parse_timestamp_ns(val)
                elif fn == 9 and expected_msg_id:
                    for ufn, uwt, uval in iter_proto_fields(val):
                        if ufn == 7 and uwt == 2:
                            step_msg_id = bytes(uval).decode("utf-8", "replace")
                            break
        if step_type == 15 and (t_create or t_done):
            if expected_msg_id and step_msg_id and step_msg_id != expected_msg_id:
                continue
            think_dur_ms = 0
            has_response = False
            has_thinking = False
            if payload_blob:
                for pfn, pwt, pval in iter_proto_fields(payload_blob):
                    if pfn == 20 and pwt == 2:
                        for rfn, rwt, rval in iter_proto_fields(pval):
                            if rwt == 2:
                                if rfn in (1, 8) and len(rval) > 0:
                                    has_response = True
                                elif rfn in (3, 16) and len(rval) > 0:
                                    has_thinking = True
                                elif rfn == 11:
                                    think_dur_ms = parse_duration_ms(rval)
                            elif rwt == 0 and rfn == 5 and rval != 0:
                                has_thinking = True
            return t_create, (t_done or t_create), think_dur_ms, has_response, has_thinking
        if not best_start and (t_create or t_done):
            best_start = t_create or t_done
            best_end = t_done or t_create
    return best_start, best_end, 0, False, False


def decode_gen_row(
    conn: sqlite3.Connection,
    db_stem: str,
    idx: int,
    blob: bytes,
    fallback_hook_model: str = "",
) -> tuple[str, dict | None]:
    """Return (status_kind, record_or_none).

    status_kind in:
      - 'ready': valid reported record ready for Magpie
      - 'skip_gateway': proxied through Magpie already, safe to advance cursor
      - 'pending_or_empty': 0 tokens (may still be in-flight if at tail)
    """
    step_indices: list[int] = []
    req_slug = ""
    m19 = ""
    m21 = ""
    msg_id = ""
    meta_map: dict[str, str] = {}
    in_tok = out_tok = cache_read = cache_write = reasoning = 0
    ttft_ms = flow_ms = 0
    has_gen_msg = False

    for fn, wt, val in iter_proto_fields(blob):
        if fn == 2 and wt == 2:
            j = 0
            n = len(val)
            while j < n:
                try:
                    sv, j = parse_varint(val, j)
                    step_indices.append(sv)
                except ValueError:
                    break
        elif fn == 3 and wt == 2:
            for fn3, wt3, val3 in iter_proto_fields(val):
                if fn3 == 28 and wt3 == 2:
                    req_slug = bytes(val3).decode("utf-8", "replace")
        elif fn == 1 and wt == 2:
            has_gen_msg = True
            for fn2, wt2, val2 in iter_proto_fields(val):
                if fn2 == 4 and wt2 == 2:
                    for ufn, uwt, uval in iter_proto_fields(val2):
                        if uwt == 0:
                            if ufn == 2:
                                in_tok = uval
                            elif ufn == 3:
                                out_tok = uval
                            elif ufn == 4:
                                cache_write = uval
                            elif ufn == 5:
                                cache_read = uval
                            elif ufn == 9:
                                # ModelUsageStats.thinking_output_tokens = 9 (response_output_tokens = 10)
                                reasoning = uval
                        elif uwt == 2 and ufn == 7:
                            msg_id = bytes(uval).decode("utf-8", "replace")
                elif fn2 == 11 and wt2 == 2:
                    ttft_ms = parse_duration_ms(val2)
                elif fn2 == 12 and wt2 == 2:
                    flow_ms = parse_duration_ms(val2)
                elif fn2 == 19 and wt2 == 2:
                    m19 = bytes(val2).decode("utf-8", "replace")
                elif fn2 == 21 and wt2 == 2:
                    m21 = bytes(val2).decode("utf-8", "replace")
                elif fn2 == 20 and wt2 == 2:
                    k = v = ""
                    for fn3, wt3, val3 in iter_proto_fields(val2):
                        if fn3 == 1 and wt3 == 2:
                            k = bytes(val3).decode("utf-8", "replace")
                        elif fn3 == 2 and wt3 == 2:
                            v = bytes(val3).decode("utf-8", "replace")
                    if k:
                        meta_map[k] = v

    if is_gateway_proxied(m19, m21):
        return "skip_gateway", None

    if not has_gen_msg or (in_tok == 0 and out_tok == 0 and cache_read == 0 and cache_write == 0):
        return "pending_or_empty", None

    model_enum = meta_map.get("model_enum", "")
    model, effort = normalize_model_and_effort(m19, m21, req_slug, model_enum, fallback_hook_model)

    if "last_step_index" in meta_map:
        try:
            lsi = int(meta_map["last_step_index"])
            step_indices.extend([lsi + 1, lsi, lsi + 2])
        except ValueError:
            pass

    start_ns, end_ns, think_dur_ms, has_response, has_thinking = extract_step_details(
        conn, step_indices, msg_id
    )
    total_ms = ttft_ms + flow_ms
    if total_ms <= 0 and end_ns > start_ns > 0:
        total_ms = int(round((end_ns - start_ns) / 1_000_000.0))

    first_text_ms = 0
    if has_response and ttft_ms > 0 and total_ms >= ttft_ms:
        if think_dur_ms > 0:
            cand_ft = max(ttft_ms, think_dur_ms) if think_dur_ms >= ttft_ms - 5 else 0
            if ttft_ms <= cand_ft <= total_ms:
                first_text_ms = cand_ft
        elif not has_thinking:
            first_text_ms = ttft_ms

    req_id = meta_map.get("request_id") or f"{db_stem}-{idx}"
    session_id = meta_map.get("root_cascade_id") or meta_map.get("cascade_id") or db_stem

    rec: dict = {
        "rid": f"agy-{req_id}",
        "t": ns_to_rfc3339(end_ns or start_ns),
        "agent": "antigravity",
        "provider": "antigravity",
        "model": model,
        "in": int(in_tok),
        "out": int(out_tok),
        "cache_read": int(cache_read),
        "status": 200,
        "session": session_id,
    }
    if cache_write > 0:
        rec["cache_write"] = int(cache_write)
    if effort:
        rec["effort"] = effort
    if reasoning > 0:
        rec["reasoning"] = int(min(reasoning, out_tok)) if out_tok > 0 else int(reasoning)
    if total_ms > 0:
        rec["ms"] = int(total_ms)
    if ttft_ms > 0:
        rec["ttft_ms"] = int(ttft_ms)
    if first_text_ms > 0:
        rec["first_text_ms"] = int(first_text_ms)
    if flow_ms > 0:
        rec["flow_ms"] = int(flow_ms)

    return "ready", rec


def load_gateway_key(caller_keys_path: Path) -> str:
    env_key = os.environ.get("MAGPIE_GATEWAY_KEY", "").strip()
    if env_key:
        return env_key
    if not caller_keys_path.is_file():
        return ""
    try:
        items = json.loads(caller_keys_path.read_text(encoding="utf-8"))
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and not item.get("disabled") and not item.get("readOnly"):
                    sec = str(item.get("secret") or "").strip()
                    if sec:
                        return sec
    except Exception:
        pass
    return ""


def post_records(url: str, key: str, records: list[dict], timeout: float = 3.0) -> dict:
    if not records:
        return {"accepted": 0, "duplicates": 0}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps(records, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        if raw.strip():
            try:
                return json.loads(raw)
            except Exception:
                return {"raw": raw}
        return {}


def db_signature(db_path: Path) -> list[int]:
    st = db_path.stat()
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        wst = wal.stat()
        return [st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size]
    return [st.st_mtime_ns, st.st_size, 0, 0]


def load_state(state_path: Path) -> dict:
    candidates = [state_path]
    fallback = _default_state_path()
    if fallback != state_path:
        candidates.append(fallback)
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("dbs"), dict):
                    return data
            except Exception:
                pass
    return {"version": 1, "dbs": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, state_path)


def sync_all(
    *,
    dry_run: bool = False,
    reset_cursor: bool = False,
    priority_conv_id: str = "",
    fallback_hook_model: str = "",
    max_seconds: float = 0.0,
    http_timeout: float = 5.0,
) -> dict:
    started = time.monotonic()
    conv_dir, state_path, caller_keys_path = _paths()
    url = os.environ.get("MAGPIE_USAGE_URL", DEFAULT_MAGPIE_URL).strip() or DEFAULT_MAGPIE_URL
    key = load_gateway_key(caller_keys_path)

    state = {"version": 1, "dbs": {}} if reset_cursor else load_state(state_path)
    dbs_state: dict[str, dict] = state.setdefault("dbs", {})

    stats = {
        "scanned_dbs": 0,
        "ready": 0,
        "posted": 0,
        "skipped_gateway": 0,
        "pending_tail": 0,
        "errors": [],
        "models": {},
    }

    if not conv_dir.is_dir():
        return stats

    db_files = [p for p in conv_dir.iterdir() if p.suffix == ".db" and p.is_file()]
    # Sync active conversation first so hook latency prioritizes current turn
    db_files.sort(
        key=lambda p: (0 if p.stem == priority_conv_id else 1, -p.stat().st_mtime_ns)
    )

    state_dirty = False

    for db_path in db_files:
        if max_seconds > 0 and (time.monotonic() - started) >= max_seconds:
            break

        stem = db_path.stem
        try:
            sig = db_signature(db_path)
        except OSError:
            continue

        prev = dbs_state.get(stem)
        if prev and prev.get("sig") == sig:
            continue

        last_idx = int(prev.get("last_idx", -1)) if prev else -1
        stats["scanned_dbs"] += 1

        try:
            uri = f"file:{db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        except Exception as exc:
            stats["errors"].append(f"open {stem}: {exc}")
            continue

        try:
            rows = conn.execute(
                "SELECT idx, data FROM gen_metadata WHERE idx > ? ORDER BY idx ASC",
                (last_idx,),
            ).fetchall()
        except Exception:
            conn.close()
            continue

        if not rows:
            conn.close()
            dbs_state[stem] = {"last_idx": last_idx, "sig": sig}
            state_dirty = True
            continue

        # Decode rows and track highest contiguous safe cursor (don't skip an in-flight tail row)
        decoded: list[tuple[int, str, dict | None]] = []
        hook_model = fallback_hook_model if stem == priority_conv_id else ""
        for idx, blob in rows:
            if not blob:
                decoded.append((idx, "pending_or_empty", None))
                continue
            kind, rec = decode_gen_row(conn, stem, idx, blob, hook_model)
            decoded.append((idx, kind, rec))
        conn.close()

        # Any 'pending_or_empty' row followed by a non-pending row is a historical aborted step, safe to pass.
        last_non_pending_pos = -1
        for pos, (_idx, kind, _rec) in enumerate(decoded):
            if kind in ("ready", "skip_gateway"):
                last_non_pending_pos = pos

        batch_records: list[dict] = []
        safe_idx = last_idx
        hit_live_tail = False

        for pos, (idx, kind, rec) in enumerate(decoded):
            if kind == "ready" and rec is not None:
                batch_records.append(rec)
                stats["ready"] += 1
                m = rec["model"]
                stats["models"][m] = stats["models"].get(m, 0) + 1
                safe_idx = idx
            elif kind == "skip_gateway":
                stats["skipped_gateway"] += 1
                safe_idx = idx
            else:
                if pos < last_non_pending_pos:
                    safe_idx = idx
                else:
                    stats["pending_tail"] += 1
                    hit_live_tail = True
                    break

        if dry_run:
            continue

        post_ok = True
        for i in range(0, len(batch_records), BATCH_SIZE):
            chunk = batch_records[i : i + BATCH_SIZE]
            try:
                post_records(url, key, chunk, timeout=http_timeout)
                stats["posted"] += len(chunk)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                stats["errors"].append(f"HTTP {exc.code} on {stem}: {detail}")
                post_ok = False
                break
            except Exception as exc:
                stats["errors"].append(f"POST {stem}: {exc}")
                post_ok = False
                break

        if post_ok:
            entry: dict = {"last_idx": safe_idx}
            if not hit_live_tail:
                entry["sig"] = sig
            dbs_state[stem] = entry
            state_dirty = True
        else:
            # Magpie unreachable or errored: stop early so hook returns immediately; cursor stays safe
            break

    if state_dirty and not dry_run:
        try:
            save_state(state_path, state)
        except Exception as exc:
            stats["errors"].append(f"save_state: {exc}")

    return stats


def hook_response(event_name: str) -> str:
    if event_name == "Stop":
        return json.dumps({"decision": "allow"})
    return "{}"


def run_hook() -> int:
    raw = sys.stdin.read()
    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

    event_name = str(payload.get("hookEventName") or "")
    conv_id = str(payload.get("conversationId") or "")
    model_name = str(payload.get("modelName") or "")

    try:
        sync_all(
            priority_conv_id=conv_id,
            fallback_hook_model=model_name,
            max_seconds=3.5,
            http_timeout=1.2,
        )
    except Exception:
        pass

    sys.stdout.write(hook_response(event_name) + "\n")
    sys.stdout.flush()
    return 0


def run_self_test() -> int:
    # 1. Varint & Timestamp
    assert parse_varint(b"\x96\x01", 0) == (150, 2)
    assert ns_to_rfc3339(1794380531_304484000) == "2026-11-11T07:02:11.304484Z"

    # 2. Model & Effort normalization
    assert normalize_model_and_effort("gemini-4-carbon", "Gemini 4 Carbon (Medium)", "", "") == (
        "gemini-4-carbon",
        "medium",
    )
    assert normalize_model_and_effort("claude-opus-5-5-high", "Claude Opus 5.5 (High)", "", "") == (
        "claude-opus-5-5",
        "high",
    )
    assert normalize_model_and_effort("", "", "", "MODEL_PLACEHOLDER_M417") == (
        "gemini-4-argon",
        "high",
    )
    assert normalize_model_and_effort("", "", "", "MODEL_PLACEHOLDER_M525") == (
        "gemini-4-hydrogen",
        "high",
    )
    assert normalize_model_and_effort("gemini-4-argon-d", "Gemini 4 Argon (Medium)", "", "") == (
        "gemini-4-argon",
        "medium",
    )

    # 3. Gateway double-counting guard
    assert is_gateway_proxied("", "[Magpie] Gemini 4 Argon (antigravity)") is True
    assert is_gateway_proxied("antigravity/gemini-4-argon", "antigravity/gemini-4-argon") is True
    assert is_gateway_proxied("gemini-4-carbon", "Gemini 4 Carbon (Medium)") is False

    # 4. Hook stdout contract
    assert json.loads(hook_response("Stop")) == {"decision": "allow"}
    assert json.loads(hook_response("PostInvocation")) == {}

    # 5. Protobuf reasoning (ufn=9) vs response (ufn=10) & first_text_ms extraction
    def _vint(v: int) -> bytes:
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def _f_var(fn: int, v: int) -> bytes:
        return _vint((fn << 3) | 0) + _vint(v)

    def _f_bytes(fn: int, b: bytes) -> bytes:
        return _vint((fn << 3) | 2) + _vint(len(b)) + b

    mem = sqlite3.connect(":memory:")
    mem.execute(
        "CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer, metadata blob, step_payload blob)"
    )
    ts_start = _f_var(1, 1794538000)
    ts_end = _f_var(1, 1794538018)
    step_meta = _f_bytes(1, ts_start) + _f_bytes(7, ts_end) + _f_bytes(9, _f_bytes(7, b"msg-1"))
    planner_resp = (
        _f_bytes(1, b"Hello world") + _f_bytes(3, b"Thinking...") + _f_bytes(11, _f_var(1, 11))
    )
    step_payload = _f_bytes(20, planner_resp)
    mem.execute("INSERT INTO steps VALUES (5, 15, ?, ?)", (step_meta, step_payload))

    usage_msg = (
        _f_var(2, 1000)
        + _f_var(3, 500)
        + _f_var(4, 50)
        + _f_var(5, 2000)
        + _f_bytes(7, b"msg-1")
        + _f_var(9, 200)
        + _f_var(10, 300)
    )
    gen_msg = (
        _f_bytes(4, usage_msg)
        + _f_bytes(11, _f_var(1, 5))
        + _f_bytes(12, _f_var(1, 13))
        + _f_bytes(19, b"gemini-4-hydrogen")
        + _f_bytes(21, b"Gemini 4 Hydrogen (High)")
    )
    gen_blob = _f_bytes(2, _vint(5)) + _f_bytes(1, gen_msg)
    kind, rec = decode_gen_row(mem, "conv-1", 0, gen_blob)
    mem.close()
    assert kind == "ready" and rec is not None
    assert rec["reasoning"] == 200
    assert rec["cache_write"] == 50
    assert rec["ttft_ms"] == 5000
    assert rec["first_text_ms"] == 11000
    assert rec["ms"] == 18000

    # 6. Environment path overrides (PLUGIN_DATA / ANTIGRAVITY_CONVERSATIONS_DIR / MAGPIE_CONFIG_DIR)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        pdata = tmp_root / "pdata"
        cdir = tmp_root / "convs"
        mcfg = tmp_root / "magpie"
        mcfg.mkdir(parents=True)
        (mcfg / "caller-keys.json").write_text("[]", encoding="utf-8")
        saved_env = {
            k: os.environ.get(k)
            for k in ("PLUGIN_DATA", "ANTIGRAVITY_CONVERSATIONS_DIR", "MAGPIE_CONFIG_DIR")
        }
        try:
            os.environ["PLUGIN_DATA"] = str(pdata)
            os.environ["ANTIGRAVITY_CONVERSATIONS_DIR"] = str(cdir)
            os.environ["MAGPIE_CONFIG_DIR"] = str(mcfg)
            got_conv, got_state, got_keys = _paths()
            assert got_conv == cdir
            assert got_state == pdata / "state.json"
            assert got_keys == mcfg / "caller-keys.json"
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    print("SELF_TEST_OK")
    return 0


def run_status() -> int:
    conv_dir, state_path, caller_keys_path = _paths()
    state = load_state(state_path)
    dbs = state.get("dbs", {})
    db_count = len([p for p in conv_dir.glob("*.db")]) if conv_dir.is_dir() else 0
    key = load_gateway_key(caller_keys_path)
    info = {
        "conversations_dir": str(conv_dir),
        "conversation_dbs": db_count,
        "tracked_dbs_in_state": len(dbs),
        "state_path": str(state_path),
        "caller_keys_path": str(caller_keys_path),
        "magpie_url": os.environ.get("MAGPIE_USAGE_URL", DEFAULT_MAGPIE_URL),
        "gateway_key_found": bool(key),
    }
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="Sync Antigravity native usage to Magpie.")
    ap.add_argument("--hook", action="store_true", help="Run as Antigravity PostInvocation/Stop hook")
    ap.add_argument("--backfill", action="store_true", help="Sync all unsynced historical conversations now")
    ap.add_argument(
        "--reset-cursor",
        action="store_true",
        help="Ignore saved cursor and re-scan all DBs (idempotent by rid)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Scan and summarize without posting to Magpie")
    ap.add_argument("--status", action="store_true", help="Print sync cursor and configuration status")
    ap.add_argument("--self-test", action="store_true", help="Run built-in unit assertions")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()
    if args.hook:
        return run_hook()
    if args.status:
        return run_status()

    result = sync_all(dry_run=args.dry_run, reset_cursor=args.reset_cursor)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
