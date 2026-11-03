#!/usr/bin/env python3
"""本地陪伴型 agent 主动开口决策器（Python 3.12，仅标准库）。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import subprocess
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
MODES = {"share", "old_topic", "care", "small_deliverable", "meme"}

# ── 可配置项。默认值等价于旧行为（cwd 下的 memory/ diary/ conversations/）。
CFG: dict[str, Any] = {
    "facts": "memory/facts.md",
    "lessons": "memory/lessons.md",
    "diary_dir": "diary",
    "conversations_dir": "conversations",
    "state_dir": "state",
    "quiet_start": 23,
    "quiet_end": 9,
    "max_per_day": 1,
    "max_per_7d": 3,
    "cooldown_after_user_min": 13,   # 双方最后一条之后安静多少分钟才允许开口
    "cooldown_after_unanswered_min": 72 * 60,
    "max_consecutive_unanswered": 2,   # 连续几条没回就彻底停
    "threshold": 0.55,
    "threshold_high": None,   # 加成后门槛；None = 与 threshold 相同
    "generator": None,     # list[str] | None → 默认用内置 dsh 调用
    "env_file": None,      # KEY=VALUE 文件，给生成器注入环境变量
    "sender": None,        # list[str] | None → 默认用 say.sh
    "weights": {"relevance":0.35,"specificity":0.25,"timeliness":0.20,
                "novelty":0.10,"chatability":0.10,"reply_burden":-0.30},
}


def load_toml_config(path: str | None) -> None:
    """把 TOML 覆盖进 CFG。字段名见 examples/config.toml。"""
    if not path:
        return
    import tomllib
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    inputs = data.get("inputs", {})
    policy = data.get("policy", {})
    paths = data.get("paths", {})
    mapping = {
        "facts": inputs.get("facts"),
        "lessons": inputs.get("lessons"),
        "diary_dir": inputs.get("diary_dir"),
        "conversations_dir": inputs.get("conversations_dir"),
        "state_dir": paths.get("state"),
        "quiet_start": policy.get("quiet_start"),
        "quiet_end": policy.get("quiet_end"),
        "max_per_day": policy.get("max_per_day"),
        "max_per_7d": policy.get("max_per_7d"),
        "cooldown_after_user_min": policy.get("cooldown_after_user_min"),
        "cooldown_after_unanswered_min": policy.get("cooldown_after_unanswered_min"),
        "max_consecutive_unanswered": policy.get("max_consecutive_unanswered"),
        "threshold": policy.get("threshold"),
        "threshold_high": policy.get("threshold_high"),
        "max_text_len": policy.get("max_text_len"),
        "activity_window_days": policy.get("activity_window_days"),
    }
    for key, value in mapping.items():
        if value is not None:
            CFG[key] = value
    if isinstance(policy.get("weights"), dict):
        CFG["weights"].update(policy["weights"])
    if isinstance(data.get("generator", {}).get("command"), list):
        CFG["generator"] = data["generator"]["command"]
    if isinstance(data.get("generator", {}).get("env_file"), str):
        CFG["env_file"] = data["generator"]["env_file"]
    if isinstance(data.get("sender", {}).get("command"), list):
        CFG["sender"] = data["sender"]["command"]

    # 2026-11-03：这两个之前是死配置（代码里写死 200 / 120 / 固定文件名）。
    for section, key in (("generator", "generator_timeout"),
                         ("sender", "sender_timeout")):
        value = data.get(section, {}).get("timeout")
        if isinstance(value, (int, float)) and value > 0:
            CFG[key] = float(value)
    if isinstance(data.get("paths", {}).get("decision_log"), str):
        CFG["decision_log"] = data["paths"]["decision_log"]

    # 2026-11-03 修：示例里写了 policy.timezone，但从来没被读过，
    # 改配置不生效。这里真正加载它；名字不合法就报错，别静默用默认值。
    tz_name = policy.get("timezone")
    if isinstance(tz_name, str) and tz_name:
        global TZ
        try:
            TZ = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"配置里的 timezone 不合法：{tz_name}（{exc}）") from exc
QUESTION_RE = re.compile(r"[?？]")
BURDEN_RE = re.compile(
    r"[?？]|你去|你要|你必须|你应该|请你|帮我|告诉我|回复我|记得去|不妨去"
)
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "我们", "你们", "他们", "这个", "那个", "一个", "可以", "就是",
    "然后", "因为", "所以", "但是", "已经", "还是", "什么", "怎么",
    "the", "and", "for", "with", "this", "that",
}


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    occurred_at: datetime
    source: str


@dataclass
class Candidate:
    hook: str
    mode: str
    source: str
    kind: str
    content: str
    occurred_at: datetime | None
    needs_rephrase: bool = False


def decision_log_path(state_dir: Path) -> Path:
    """日志文件位置。2026-11-03：让它真的跟 paths.decision_log 走，
    原来这个配置项读了没人用，写在 state 目录下是写死的。"""
    name = CFG.get("decision_log")
    return state_dir / (Path(name).name if name else "decisions.jsonl")


def hook_fingerprint(text: str, length: int = 12) -> str:
    """由头指纹：用来去重和审计，但不可逆，不泄漏原文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt).astimezone(TZ)
    except (ValueError, OverflowError):
        return None


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False
        ) as file:
            temporary = file.name
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


def read_text(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        errors.append(f"{path}: {type(exc).__name__}: {exc}")
        return ""


def read_jsonl(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(read_text(path, errors).splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("记录不是对象")
            records.append(item)
        except (ValueError, TypeError) as exc:
            errors.append(f"{path}:{number}: {exc}")
    return records


def load_state(path: Path, errors: list[str]) -> dict[str, Any]:
    text = read_text(path, errors)
    if not text.strip():
        return {"version": 1, "sends": []}
    try:
        state = json.loads(text)
        if not isinstance(state, dict) or not isinstance(state.get("sends"), list):
            raise ValueError("state 必须包含 sends 数组")
        return state
    except (ValueError, TypeError) as exc:
        errors.append(f"{path}: {exc}")
        return {"version": 1, "sends": []}


def tokenize(text: str) -> set[str]:
    tokens = set(WORD_RE.findall(text.lower()))
    for run in CHINESE_RE.findall(text):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens - STOPWORDS


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)]|#{1,6}|>)\s+", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def snippet(text: str, maximum: int = 100) -> str:
    text = clean_markdown(text)
    sentence = re.split(r"(?<=[。！？!?])", text, maxsplit=1)[0]
    if len(sentence) > maximum:
        sentence = sentence[:maximum - 1].rstrip("，,；;：: ") + "…"
    return sentence.strip()


def choose_mode(content: str, kind: str) -> str:
    if re.search(r"表情包|段子|笑话|哈哈|梗", content):
        return "meme"
    if re.search(r"失眠|疲惫|累了|生病|难过|压力|焦虑|休息|睡眠", content):
        return "care"
    if kind == "lessons" or re.search(r"清单|步骤|模板|方法|技巧|备忘", content):
        return "small_deliverable"
    return "old_topic" if kind == "conversations" else "share"


def make_hook(content: str, kind: str, mode: str) -> str:
    """hook 是「给模型的由头」，不是最终文案。

    旧版把 hook 当成品，拼出「想起你之前说的『原话』」这种复读句。
    现在 hook 只描述这次开口的着力点，最终那句话交给生成阶段重写。
    """
    quoted = content.rstrip("。").strip()
    if mode == "care":
        return f"他提过这件事：{quoted}｜可以顺着关心一句，别追问，别要他回复。"
    if mode == "meme":
        return f"有个可用的梗或段子：{quoted}｜可以拿来逗一句。"
    if mode == "small_deliverable":
        return f"手上有一条能直接给的小结论：{quoted}｜可以把它讲清楚给他。"
    if kind == "conversations":
        # 2026-11-03 修：原来这里断言"这事还悬着"，可是代码根本不知道
        # 后来有没有解决 —— 会把早就答完的问题再追一遍。
        # 不假定状态，让生成器自己判断值不值得接。
        return (f"他之前说过的：{quoted}｜不要假定这件事还没解决，"
                f"也不要原样复述他的原话。")
    if kind == "diary":
        return f"我日记里写过：{quoted}｜这是我自己的事，可以说给他听。"
    return f"记下来的一条：{quoted}｜可以拿出来说一句。"


def load_messages(
    root: Path, now: datetime, errors: list[str]
) -> list[Message]:
    messages: list[Message] = []
    # 全量读取以免漏掉较早主动消息之后、近期窗口之前的回复。
    for path in sorted((root / CFG["conversations_dir"]).glob("*.jsonl")):
        for index, record in enumerate(read_jsonl(path, errors), 1):
            dt = timestamp(record.get("occurredAt"))
            role = record.get("role")
            content = record.get("content")
            if dt is None or role not in {"user", "assistant"} or not isinstance(content, str):
                errors.append(f"{path}:记录 {index}: 对话字段无效")
                continue
            if dt <= now:
                messages.append(Message(role, content, dt, f"{path.relative_to(root)}:{index}"))
    return sorted(messages, key=lambda message: message.occurred_at)


def generate_candidates(
    root: Path, messages: list[Message], now: datetime, errors: list[str]
) -> list[Candidate]:
    candidates: list[Candidate] = []

    def add(text: str, kind: str, source: str, dt: datetime | None) -> None:
        content = snippet(text)
        if len(content) < 4 or not tokenize(content):
            return
        mode = choose_mode(content, kind)
        candidates.append(Candidate(
            make_hook(content, kind, mode), mode, source, kind, content, dt
        ))

    for kind in ("facts", "lessons"):
        path = root / CFG[f"{kind}"]
        for number, line in enumerate(read_text(path, errors).splitlines(), 1):
            if re.match(r"^\s*-\s+", line):
                add(line, kind, f"memory/{kind}.md:{number}", None)

    for path in sorted((root / CFG["diary_dir"]).glob("*.md")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day > now.date():
            continue
        dt = datetime.combine(day, time.min, TZ)
        in_code = False
        for number, line in enumerate(read_text(path, errors).splitlines(), 1):
            if line.lstrip().startswith(("```", "~~~")):
                in_code = not in_code
                continue
            if in_code or not line.strip() or line.lstrip().startswith("#"):
                continue
            add(line, "diary", f"diary/{path.name}:{number}", dt)

    for message in messages:
        # assistant 的话不能被当成用户的事实或经历。
        if message.role != "user":
            continue
        # 2026-11-03：窗口天数改成可配（示例里的 activity_window_days 之前是死配置）。
        if message.occurred_at < now - timedelta(
                days=int(CFG.get("activity_window_days") or 28)):
            continue
        body = (message.content or "").strip()
        if len(body) < 8:
            continue
        # 用户原话不能直接复读。只有「带问号」或「有意思的具体主张」才留作旧话题引子，
        # 并且标记 mode=old_topic，交给生成阶段重说，不许原样发回。
        if "？" in body or "?" in body:
            cand = Candidate(
                make_hook(body, "conversations", "old_topic"),
                "old_topic", message.source, "conversations", snippet(body),
                message.occurred_at,
            )
            cand.needs_rephrase = True
            candidates.append(cand)
        elif len(body) >= 16 and not body.startswith(("/", "#")):
            cand = Candidate(
                make_hook(body, "conversations", "old_topic"),
                "old_topic", message.source, "conversations", snippet(body),
                message.occurred_at,
            )
            cand.needs_rephrase = True
            candidates.append(cand)

    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.hook[:40]
        previous = unique.get(key)
        if previous is None or (
            candidate.occurred_at or datetime.min.replace(tzinfo=TZ)
        ) > (previous.occurred_at or datetime.min.replace(tzinfo=TZ)):
            unique[key] = candidate
    return list(unique.values())


def activity_weights(messages: list[Message], now: datetime) -> dict[str, list[float]]:
    # 使用此前若干个完整自然日，避免当前日尚未结束造成偏差。
    window_days = int(CFG.get("activity_window_days") or 28)
    start_day = now.date() - timedelta(days=window_days)
    selected = [
        message for message in messages
        if message.role == "user"
        and start_day <= message.occurred_at.date() < now.date()
    ]
    if len({message.occurred_at.date() for message in selected}) < 5:
        return {"weekday": [0.6] * 12, "weekend": [0.6] * 12}

    result: dict[str, list[float]] = {}
    for name, weekend in (("weekday", False), ("weekend", True)):
        grouped: dict[Any, list[int]] = {}
        for message in selected:
            if (message.occurred_at.weekday() >= 5) != weekend:
                continue
            counts = grouped.setdefault(message.occurred_at.date(), [0] * 12)
            counts[message.occurred_at.hour // 2] += 1
        if len(grouped) < 5:
            result[name] = [0.6] * 12
            continue
        totals = [0.0] * 12
        for counts in grouped.values():
            total = sum(counts)
            for index, count in enumerate(counts):
                totals[index] += count / total
        maximum = max(totals)
        result[name] = [value / maximum if maximum else 0.6 for value in totals]
    return result


def send_history(
    state: dict[str, Any], records: list[dict[str, Any]],
    now: datetime, errors: list[str]
) -> list[dict[str, Any]]:
    combined = list(state["sends"])
    combined.extend(
        record for record in records
        if record.get("decision") == "SEND" and not record.get("dry_run")
        and record.get("type") != "DRYRUN"
    )
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in combined:
        if not isinstance(item, dict):
            errors.append("无效 SEND 历史：不是对象")
            continue
        dt = timestamp(item.get("time"))
        mode = item.get("mode")

        # 2026-11-03 修：这里原来读 item["hook"]，但写入端存的是 hook_id，
        # 于是每一次成功发送之后，下一轮都会把所有历史判成无效 →
        # errors 非空 → 强制 SILENT → 永久停机。字段名必须统一成 hook_id。
        fp = item.get("hook_id")
        if fp is None:
            # 兼容更早的旧记录：那时存的是 hook 原文（或它的指纹）。
            legacy = item.get("hook")
            if isinstance(legacy, str):
                fp = (legacy if re.fullmatch(r"[0-9a-f]{12}", legacy)
                      else hook_fingerprint(legacy))
        if dt is None or not isinstance(fp, str) or mode not in MODES:
            errors.append("无效 SEND 历史：time/hook_id/mode 缺失或无效")
            continue
        if dt > now:
            errors.append("SEND 历史时间在未来，可能发生系统时钟回拨")
            continue

        has_question = item.get("has_question")
        if not isinstance(has_question, bool):
            # 旧记录没这个字段。判断不出来时按"含问号"处理，
            # 宁可收紧配额，也不要因为缺字段就放宽。
            has_question = True

        normalized = {
            "time": iso(dt), "hook_id": fp, "mode": mode,
            "has_question": has_question,
        }
        unique[(iso(dt), fp)] = normalized
    return sorted(unique.values(), key=lambda item: item["time"])


def duplicate_reason(candidate: Candidate, history: list[dict[str, Any]]) -> str | None:
    recent = history[-10:]
    fp = hook_fingerprint(candidate.hook)
    if any(item.get("hook_id") == fp for item in recent):
        return "hook 前 40 字重复"
    if recent and recent[-1]["mode"] == candidate.mode:
        return "mode 不得连续两次相同"
    if len(recent) >= 2 and all(item["mode"] == candidate.mode for item in recent[-2:]):
        return "最近三条不得全部为同一 mode"
    if QUESTION_RE.search(candidate.hook):
        if sum(bool(item.get("has_question")) for item in recent[-4:]) >= 2:
            return "最近五条最多两条含问号"
    return None


def score_candidate(
    candidate: Candidate, recent_tokens: list[set[str]],
    history: list[dict[str, Any]], now: datetime, activity: float
) -> dict[str, float]:
    tokens = tokenize(candidate.content)
    # 与每条近期用户消息求 Jaccard，取最大值，避免长窗口词并集稀释。
    relevance = max((jaccard(tokens, other) for other in recent_tokens), default=0.0)
    # 从用户原话派生的候选，自己是自己的来源，Jaccard 必然满分，必须打掉这部分虚高。
    if candidate.needs_rephrase:
        relevance *= 0.55
    specificity = min(1.0, 0.25 + len(tokens) / 45)
    if re.search(r"\d|《[^》]+》|「[^」]+」|[A-Za-z]{3,}", candidate.content):
        specificity = min(1.0, specificity + 0.15)
    if candidate.occurred_at is None:
        timeliness = 0.35 if candidate.kind == "facts" else 0.40
    else:
        days = max(0.0, (now - candidate.occurred_at).total_seconds() / 86400)
        timeliness = 0.45 + 0.55 * math.exp(-days / 7)
    # 2026-11-03 修：send_history() 只保留 hook_id 指纹，不保留分好的词，
    # 所以 item["_tokens"] 永远是空的 —— 原来这里实际恒定返回 1.0，
    # 等于所有候选白拿 0.10 分。算不出来就如实给 0，并让它在分数里不起作用。
    hist_tokens = [item["_tokens"] for item in history[-10:] if item.get("_tokens")]
    if hist_tokens:
        novelty = 1.0 - max(jaccard(tokens, t) for t in hist_tokens)
    else:
        novelty = 0.0
    burden = 1.0 if BURDEN_RE.search(candidate.hook) else 0.0

    # 2026-11-03 修：CFG["weights"] 之前读了但没用，配置改了没效果。
    w = CFG["weights"]
    weight_novelty = 0.0 if not hist_tokens else w["novelty"]
    score = (
        w["relevance"] * relevance + w["specificity"] * specificity
        + w["timeliness"] * timeliness + weight_novelty * novelty
        + w["chatability"] * activity + w["reply_burden"] * burden
    )
    # 中档候选需由当前工作日/周末时段的历史活跃度补到高档门槛。
    bonus = 0.20 * activity if 0.55 <= score < 0.75 else 0.0
    return {
        "relevance": relevance, "specificity": specificity,
        "timeliness": timeliness, "novelty": novelty,
        "novelty_known": bool(hist_tokens),
        "chatability": activity, "reply_burden": burden,
        "score": score, "activity_bonus": bonus, "adjusted_score": score + bonus,
    }


def evaluate(root: Path, now: datetime, dry_run: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: list[str] = []
    state_dir = root / CFG["state_dir"]
    state = load_state(state_dir / "state.json", errors)
    records = read_jsonl(decision_log_path(state_dir), errors)
    history = send_history(state, records, now, errors)
    messages = load_messages(root, now, errors)
    candidates = generate_candidates(root, messages, now, errors)
    weights = activity_weights(messages, now)
    category = "weekend" if now.weekday() >= 5 else "weekday"
    activity = weights[category][now.hour // 2]
    recent_users = [
        message for message in messages
        if message.role == "user" and message.occurred_at >= now - timedelta(days=7)
    ]
    recent_tokens = [tokenize(message.content) for message in recent_users]
    ranked: list[tuple[Candidate, dict[str, float]]] = []
    rejected: dict[str, int] = {}
    for candidate in candidates:
        reason = duplicate_reason(candidate, history)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        ranked.append((
            candidate,
            score_candidate(candidate, recent_tokens, history, now, activity)
        ))
    ranked.sort(key=lambda pair: (pair[1]["adjusted_score"], pair[1]["score"]), reverse=True)
    low = CFG["threshold"]
    high = CFG.get("threshold_high")
    if high is None:
        # 默认：加成后门槛不超过原始分门槛 +0.20；只有配了高门槛才要求两段
        high = low
    eligible = [
        pair for pair in ranked
        if pair[1]["score"] >= low and pair[1]["adjusted_score"] >= high
    ]
    best = (eligible or ranked)[0] if ranked else None

    reasons: list[str] = []
    decision = "SEND"
    if errors:
        decision = "SILENT"
        reasons.append("本地数据读取或校验异常，保守停止发送")
    qs, qe = CFG["quiet_start"], CFG["quiet_end"]
    in_quiet = (now.hour >= qs or now.hour < qe) if qs > qe else (qs <= now.hour < qe)
    if not dry_run and in_quiet:
        decision = "SILENT"
        reasons.append(f"静默时段 {qs:02d}:00-{qe:02d}:00")
    if sum(1 for item in history if timestamp(item["time"]).date() == now.date()) >= CFG["max_per_day"]:
        decision = "SILENT"
        reasons.append(f"今日主动消息已达 {CFG['max_per_day']} 条")
    if sum(timestamp(item["time"]) > now - timedelta(days=7) for item in history) >= CFG["max_per_7d"]:
        decision = "SILENT"
        reasons.append(f"滚动 7 天主动消息已达 {CFG['max_per_7d']} 条")
    if history:
        last_time = timestamp(history[-1]["time"])
        assert last_time is not None
        replied = any(
            message.role == "user" and message.occurred_at > last_time
            for message in messages
        )
        if not replied and now - last_time < timedelta(minutes=CFG["cooldown_after_unanswered_min"]):
            decision = "SILENT"
            hours = CFG["cooldown_after_unanswered_min"] // 60
            reasons.append(f"上一条主动消息未获回复且未满 {hours} 小时")
        if not replied:
            streak = 0
            for item in reversed(history):
                t = timestamp(item["time"])
                if t is None:
                    break
                if any(m.role == "user" and m.occurred_at > t for m in messages):
                    break
                streak += 1
            if streak >= CFG.get("max_consecutive_unanswered", 2):
                decision = "SILENT"
                reasons.append(f"连续 {streak} 条主动消息未获回复，暂停直到对方先开口")
    # 2026-11-03 改：判断的是"此刻在不在聊"，不是"用户在线多久"。
    # 旧版看"用户最近 90 分钟有没有说话"，结果白天一直在聊就整天让位，一次都发不出。
    gap = timedelta(minutes=CFG["cooldown_after_user_min"])
    last_any = max((m.occurred_at for m in messages), default=None)
    if last_any is not None and now < last_any + gap:
        if decision != "SILENT":
            decision = "DEFER"
        wait = int((last_any + gap - now).total_seconds() // 60) + 1
        reasons.append(f"最后一条消息后未满 {CFG['cooldown_after_user_min']} 分钟，再等约 {wait} 分钟")
    if decision == "SEND":
        if eligible:
            reasons.append(
                f"候选原始分 {eligible[0][1]['score']:.2f}，加成后 "
                f"{eligible[0][1]['adjusted_score']:.2f}（门槛 {low}/{high}）"
            )
        else:
            decision = "SILENT"
            reasons.append("无通过去重及评分门槛的候选")

    record: dict[str, Any] = {
        "time": iso(now),
        "type": "DRYRUN" if dry_run else "DECISION",
        "dry_run": dry_run,
        "decision": decision,
        "reasons": reasons,
        "candidate_summary": None,
        "score": None,
        "activity": {"category": category, "slot": now.hour // 2, "weight": activity},
        "candidate_count": len(candidates),
        "rejected": rejected,
        "errors": errors,
    }
    if best:
        candidate, components = best
        record.update({
            # 不记原文：只记指纹、长度、来源类型。原文留在你的记忆文件里。
            "hook_id": hook_fingerprint(candidate.hook),
            "hook_len": len(candidate.hook),
            "mode": candidate.mode,
            "source_kind": candidate.source.split("/")[0],
            "score": round(components["score"], 6),
            "scores": {key: round(value, 6) for key, value in components.items()},
        })
        record["_hook"] = candidate.hook   # 只在内存里用，写日志前会删掉
    state["version"] = 1
    state["sends"] = history
    return record, state



def generate_text(record: dict[str, Any], now: datetime) -> str:
    """把 hook 交给 headless agent，让它说成人话。说不出来返回空串。"""
    import subprocess

    hook = record.get("_hook") or ""
    slot = record.get("slot") or ""
    prompt = f"""现在 {now.strftime('%m月%d日 %H:%M')}{('，' + slot) if slot else ''}。

你手上有一个由头，用它主动跟对方说一句：

由头：{hook}

要求：
1. 一到两句，短，像人随口说的。
2. 必须贴着这个由头，别绕开它去说「在忙吗」这种空话。
3. 别提这是自动的，别提时间点和定时任务。
4. 不用「首先/其次」，不用「希望对你有帮助」，不列点。
5. 说不出来就只输出 -- 。"""

    env = dict(os.environ)
    # 生成器可能需要额外的环境变量（API key 之类）。
    # 通过 [generator] env_file 指定一个 KEY=VALUE 文件，别把密钥写进配置。
    envfile = CFG.get("env_file")
    if envfile and os.path.exists(envfile):
        for line in open(envfile):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    gen = CFG.get("generator") or ["/usr/local/bin/dsh", "--profile", "headless"]
    try:
        p = subprocess.run(
            [*gen, prompt],
            capture_output=True, text=True,
            timeout=float(CFG.get("generator_timeout") or 200), env=env,
        )
    except Exception:
        return ""
    # 2026-11-03 修：原来不看 returncode，也不看行数，直接取 stdout 最后一行。
    # 生成器一旦报错，最后一行可能是错误信息或者半句话，会被当成消息发出去。
    if p.returncode != 0:
        return ""
    lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
    # 多行输出不猜哪一行是正文，直接判成失败。
    if len(lines) != 1:
        return ""
    out = lines[0]
    if out == "--":
        return ""
    limit = int(CFG.get("max_text_len") or 300)
    if len(out) > limit:
        return ""
    # 控制字符混进来通常意味着拿到的不是正文。
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in out):
        return ""
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="仅记录 DRYRUN，跳过静默时段")
    parser.add_argument("--config", default=None, help="TOML 配置文件路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印决策和原因，不写文件、不发消息（同 --test）")
    args = parser.parse_args()
    args.test = args.test or getattr(args, "dry_run", False)
    load_toml_config(args.config)
    root = Path.cwd()
    state_dir = root / CFG["state_dir"]
    now = datetime.now(TZ)
    logged = False
    try:
        if args.test:
            # dry-run / --test：只读预演。不建目录、不写锁、不落盘。
            record, _state = evaluate(root, now, True)
            print(f"{record['decision']}  {'; '.join(record.get('reasons', []))}")
            print(f"  hook_id: {record.get('hook_id')}  ({record.get('hook_len')} 字，内容不显示)")
            print(f"  score: {record.get('score')}")
            return 0
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / ".lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            now = datetime.now(TZ)
            record, state = evaluate(root, now, args.test)
            if args.test:
                # --test：只打印决策和原因，不写日志、不写 state、不发消息。
                print(f"{record['decision']}  {'; '.join(record.get('reasons', []))}")
                print(f"  hook_id: {record.get('hook_id')}  ({record.get('hook_len')} 字，内容不显示)")
                print(f"  score: {record.get('score')}")
                return 0
            state["last_decision"] = {
                "time": record["time"], "decision": record["decision"],
                "reasons": record["reasons"],
            }
            sent_ok = False
            if record["decision"] == "SEND":
                text = generate_text(record, now)
                if not text:
                    # 生成失败：降级为 SILENT，不占配额、不入发送历史。
                    record["decision"] = "SILENT"
                    record["reasons"].append("生成阶段没说出口")
                else:
                    # 发送器由配置给。没配就别发：默认指向一个不存在的路径
                    # 只会在日志里刷错误，不如直接判成配置缺失。
                    sender = CFG.get("sender")
                    if not sender:
                        record["decision"] = "SILENT"
                        record["reasons"].append("没配 sender")
                    else:
                        try:
                            r = subprocess.run(
                                [*sender, text], capture_output=True, text=True,
                                timeout=float(CFG.get("sender_timeout") or 120))
                            if r.returncode == 0:
                                sent_ok = True
                            else:
                                record["decision"] = "SILENT"
                                record["reasons"].append(f"sender 退出 {r.returncode}")
                        except subprocess.TimeoutExpired:
                            # 超时的异常字符串里带着完整 argv，而 argv 里就是待发正文。
                            # 只记类型，不记参数。
                            record["decision"] = "SILENT"
                            record["reasons"].append("sender 超时，投递结果未知")
                        except OSError as exc:
                            record["decision"] = "SILENT"
                            record["reasons"].append(f"sender 启动失败：{type(exc).__name__}")
                        except Exception as exc:  # noqa: BLE001
                            record["decision"] = "SILENT"
                            record["reasons"].append(f"sender 异常：{type(exc).__name__}")
                    if sent_ok:
                        # 2026-11-03 修：正文只在真的发出去之后才落到 record/日志。
                        # 原来先写 record["text"] 再判能不能发，于是"没发出去的话"
                        # 也被存进了 decisions.jsonl —— 隐私声明不成立。
                        record["text"] = text
                        atomic_write(state_dir / "outbox.txt", text + "\n")
                        print(text, flush=True)
            # 只有真的发出去了才计入发送历史（配额、去重、未回复门禁都靠它）。
            if sent_ok:
                state["sends"].append({
                    "time": record["time"],
                    "hook_id": record.get("hook_id"),
                    "mode": record["mode"],
                    "has_question": bool(QUESTION_RE.search(record.get("text") or "")),
                })
            # 决策日志最后写，写的是最终结果，不是计划。
            # 下划线开头的键只用于进程内传递，不入日志。
            append_record(decision_log_path(state_dir),
                          {k: v for k, v in record.items() if not k.startswith("_")})
            logged = True
            atomic_write(
                state_dir / "state.json",
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            )
        return 0
    except Exception as exc:
        failure = {
            "time": iso(datetime.now(TZ)),
            "type": "DRYRUN" if args.test else "ERROR",
            "dry_run": args.test,
            "decision": "SILENT",
            "reasons": ["执行异常；不重试发送"],
            "candidate_summary": None,
            "score": None,
            "decision_already_logged": logged,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
        # 2026-11-03 修：--test / --dry-run 承诺"不写文件"，但异常路径原来
        # 无条件落盘（还会顺手建出 state 目录）。dry-run 时只报 stderr。
        if args.test:
            try:
                sys.stderr.write(json.dumps(failure, ensure_ascii=False) + "\n")
            except Exception:
                pass
            return 1
        try:
            append_record(decision_log_path(state_dir), failure)
        except Exception as log_exc:
            # 状态目录不可写时无法落盘，最后退回 stderr，不向 stdout 输出。
            try:
                sys.stderr.write(json.dumps(failure, ensure_ascii=False) + "\n")
                sys.stderr.write(f"日志写入失败: {type(log_exc).__name__}: {log_exc}\n")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())