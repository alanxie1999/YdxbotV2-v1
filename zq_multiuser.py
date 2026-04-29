"""
zq_multiuser.py - 多用户版本核心逻辑
版本: 2.4.3
日期: 2026-02-21
功能: 多用户押注、结算、命令处理
"""

import logging
import asyncio
import json
import os
import re
import time
from html import escape as escape_html
import requests
import aiohttp
from types import SimpleNamespace
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
from user_manager import UserContext, UserState, trim_bet_sequence_log
from typing import Dict, Any, List, Optional
import constants
from update_manager import (
    get_current_repo_info,
    list_version_catalog,
    reback_to_version,
    resolve_systemd_service_name,
    restart_process,
    update_to_version,
)

# 日志配置
logger = logging.getLogger('zq_multiuser')
logger.setLevel(logging.DEBUG)
logger.propagate = False

ACCOUNT_LOG_ROOT = os.path.join("logs", "accounts")
ACCOUNT_LOG_BACKUP_DAYS = 3
_ACCOUNT_SLUG_REGISTRY: Dict[str, str] = {}


def _sanitize_account_slug(text: str, fallback: str = "unknown") -> str:
    raw = str(text or "").strip().lower().replace(" ", "-")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return cleaned or fallback


def _build_account_label(account_slug: str) -> str:
    return f"ydx-{account_slug}"


def _resolve_user_ctx_log_slug(user_ctx: UserContext) -> str:
    slug = str(getattr(user_ctx, "account_slug", "") or "").strip()
    if slug:
        return slug
    user_id = str(getattr(user_ctx, "user_id", 0) or 0)
    return _sanitize_account_slug("", fallback=(f"user-{user_id}" if user_id not in {"", "0"} else "unknown"))


def _resolve_account_identity(
    user_ctx: Optional[UserContext] = None,
    user_id: Any = 0,
    account_name: str = "",
) -> Dict[str, str]:
    user_id_text = str(user_id or 0)
    resolved_name = str(account_name or "").strip()
    account_slug = _ACCOUNT_SLUG_REGISTRY.get(user_id_text, "").strip()
    if user_ctx is not None:
        user_id_text = str(getattr(user_ctx, "user_id", user_id_text) or user_id_text)
        if not account_slug:
            account_slug = _resolve_user_ctx_log_slug(user_ctx)
        if not resolved_name:
            resolved_name = str(getattr(getattr(user_ctx, "config", None), "name", "") or "").strip()
    if not account_slug:
        if not resolved_name and user_id_text not in {"", "0"}:
            resolved_name = f"user-{user_id_text}"
        account_slug = _sanitize_account_slug(
            resolved_name,
            fallback=(f"user-{user_id_text}" if user_id_text not in {"", "0"} else "unknown"),
        )
    return {
        "user_id": user_id_text,
        "account_name": resolved_name,
        "account_slug": account_slug,
        "account_label": _build_account_label(account_slug),
        "account_tag": f"【ydx-{account_slug}】",
    }


def register_user_log_identity(user_ctx: UserContext) -> str:
    """注册账号日志标识，供统一日志前缀和分流使用。"""
    user_id = str(getattr(user_ctx, "user_id", 0) or 0)
    account_slug = _resolve_user_ctx_log_slug(user_ctx)
    _ACCOUNT_SLUG_REGISTRY[user_id] = account_slug
    return account_slug


def _verbose_runtime_diag_enabled() -> bool:
    value = str(os.getenv("YDXBOT_VERBOSE_RUNTIME_LOGS", "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _infer_log_category(level: int, module: str, event: str) -> str:
    if level >= logging.WARNING:
        return "warning"
    text = f"{module}:{event}".lower()
    business_tokens = (
        "bet", "settle", "risk", "predict", "user_cmd", "balance", "fund",
        "profit", "preset", "pause", "resume", "restart", "update", "reback",
        "model", "apikey", "stats", "status", "yc", "dashboard",
    )
    if any(token in text for token in business_tokens):
        return "business"
    return "runtime"


class _LogDefaultsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "user_id"):
            record.user_id = "0"
        if not hasattr(record, "mod"):
            record.mod = "zq"
        if not hasattr(record, "event"):
            record.event = "general"
        if not hasattr(record, "data"):
            record.data = ""
        if not hasattr(record, "category"):
            record.category = _infer_log_category(record.levelno, str(record.mod), str(record.event))
        if not hasattr(record, "account_slug"):
            fallback_slug = f"user-{record.user_id}" if str(record.user_id) != "0" else "unknown"
            record.account_slug = _sanitize_account_slug("", fallback=fallback_slug)
        if not hasattr(record, "account_tag"):
            record.account_tag = f"【ydx-{record.account_slug}】"
        return True


class _AccountCategoryRouterHandler(logging.Handler):
    """按账号+分类分流到独立日志文件：logs/accounts/<账号>/<runtime|warning|business>.log"""

    def __init__(self, root_dir: str, backup_count: int = ACCOUNT_LOG_BACKUP_DAYS):
        super().__init__(level=logging.DEBUG)
        self.root_dir = root_dir
        self.backup_count = backup_count
        self._handlers: Dict[tuple, TimedRotatingFileHandler] = {}
        self._default_filter = _LogDefaultsFilter()
        self._formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(mod)s:%(event)s] %(message)s | %(data)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    def _get_handler(self, account_slug: str, category: str) -> TimedRotatingFileHandler:
        key = (account_slug, category)
        if key in self._handlers:
            return self._handlers[key]

        account_dir = os.path.join(self.root_dir, account_slug)
        os.makedirs(account_dir, exist_ok=True)
        log_path = os.path.join(account_dir, f"{category}.log")
        handler = TimedRotatingFileHandler(
            log_path,
            when='midnight',
            interval=1,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        handler.setFormatter(self._formatter)
        handler.addFilter(self._default_filter)
        self._handlers[key] = handler
        return handler

    def emit(self, record: logging.LogRecord):
        try:
            self._default_filter.filter(record)
            account_slug = str(getattr(record, "account_slug", "unknown") or "unknown")
            category = str(getattr(record, "category", "runtime") or "runtime")
            handler = self._get_handler(account_slug, category)
            handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self):
        for handler in self._handlers.values():
            try:
                handler.close()
            except Exception:
                pass
        self._handlers.clear()
        super().close()


_default_log_filter = _LogDefaultsFilter()

file_handler = TimedRotatingFileHandler('bot.log', when='midnight', interval=1, backupCount=7, encoding='utf-8')
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] [%(mod)s:%(event)s] %(message)s | %(data)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
file_handler.addFilter(_default_log_filter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | [%(category)s] [%(account_tag)s] %(message)s',
    datefmt='%H:%M:%S'
))
console_handler.addFilter(_default_log_filter)
logger.addHandler(console_handler)

account_category_handler = _AccountCategoryRouterHandler(ACCOUNT_LOG_ROOT, backup_count=ACCOUNT_LOG_BACKUP_DAYS)
account_category_handler.addFilter(_default_log_filter)
logger.addHandler(account_category_handler)


class _AccountIdentityFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        account_slug = str(getattr(record, "account_slug", "") or "").strip()
        if not account_slug:
            fallback_slug = f"user-{getattr(record, 'user_id', '0')}" if str(getattr(record, "user_id", "0")) != "0" else "unknown"
            account_slug = _sanitize_account_slug("", fallback=fallback_slug)
            record.account_slug = account_slug
        record.account_label = _build_account_label(account_slug)
        record.account_tag = f"【ydx-{account_slug}】"
        return True


_account_identity_filter = _AccountIdentityFilter()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(account_label)s | %(message)s',
    datefmt='%H:%M:%S'
))
console_handler.setLevel(logging.INFO)
console_handler.addFilter(_account_identity_filter)
account_category_handler.addFilter(_account_identity_filter)
try:
    logger.removeHandler(file_handler)
    file_handler.close()
except Exception:
    pass

# 自动统计推送节奏：每 10 局一次，保留 10 分钟后自动删除
AUTO_STATS_INTERVAL_ROUNDS = 10
AUTO_STATS_DELETE_DELAY_SECONDS = 600

# 风控节奏：以最近 40 笔实盘胜率为基础，结合连输深度做分层暂停。
RISK_WINDOW_BETS = 40
RISK_BASE_TRIGGER_WINS = 15          # 15/40=37.5%
RISK_BASE_TRIGGER_STREAK_NEEDED = 2   # 连续2次命中基础风控才触发暂停
RISK_RECOVERY_WINS = 19              # >45% => 至少 19/40
RISK_RECOVERY_PASS_NEEDED = 2         # 连续2次满足恢复条件才重置风险周期

# 深度风控触发节奏（不占基础风控预算）：
# 每连输 3 局触发一次；首次触发上限更高，后续触发保持保守暂停。
RISK_DEEP_TRIGGER_INTERVAL = 3
RISK_DEEP_FIRST_MAX_PAUSE_ROUNDS = 5
RISK_DEEP_NEXT_MAX_PAUSE_ROUNDS = 3
# 长龙盘面下，深度风控做“小幅放宽”，避免长时间停摆。
RISK_DEEP_LONG_DRAGON_TAIL_LEN = 5
RISK_DEEP_LONG_DRAGON_MAX_PAUSE_ROUNDS = 2
RISK_BASE_MAX_PAUSE_ROUNDS = 10

# 基础风控预算：同一基础风控周期累计暂停不超过10局（深度风控不占用）
RISK_PAUSE_TOTAL_CAP_ROUNDS = 10
RISK_PAUSE_MODEL_TIMEOUT_SEC = 5.0
AI_KEY_WARNING_TEXT = "⚠️ 大模型AI key 失效/缺失，请更新 key！！！"

# 高倍入场质量门控（目标：尽量减少进入第5手以后）
ENTRY_GUARD_STEP3_MIN_CONF = 68
ENTRY_GUARD_STEP3_PAUSE_ROUNDS = 2
ENTRY_GUARD_STEP4_MIN_CONF = 70
ENTRY_GUARD_STEP4_MIN_CONF_EARLY = 68
ENTRY_GUARD_STEP4_PAUSE_ROUNDS = 3
ENTRY_GUARD_STEP4_ALLOWED_TAGS = {"DRAGON_CANDIDATE", "SINGLE_JUMP", "SYMMETRIC_WRAP"}
UNSTABLE_PATTERN_TAGS = {"CHAOS_SWITCH", "SINGLE_JUMP", "SYMMETRIC_WRAP"}
HIGH_PRESSURE_SKIP_MIN_STEP = 5
HIGH_PRESSURE_SKIP_MIN_CONF = 78
UNSTABLE_PATTERN_MIN_CONF_STEP3 = 72
UNSTABLE_PATTERN_MIN_CONF_STEP5 = 78
DRAGON_CANDIDATE_MIN_TAIL_STEP5 = 4
NEUTRAL_LONG_TERM_GAP_LOW = 0.47
NEUTRAL_LONG_TERM_GAP_HIGH = 0.53
HIGH_PRESSURE_PATTERN_PAUSE_ROUNDS = 2

# 高阶入场二次确认（第7手起，避免第5/6手过早双模型互卡）
HIGH_STEP_DOUBLE_CONFIRM_MIN_STEP = 7
HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF = 70
HIGH_STEP_DOUBLE_CONFIRM_PAUSE_ROUNDS = 2
HIGH_STEP_DOUBLE_CONFIRM_MODEL_TIMEOUT_SEC = 5.0

# 纯交替增强：当最近盘口“由近到远”出现 6 位纯交替时，
# 主脚本强制按最新一手同向下注，尝试结束交替。
ALTERNATION_BREAK_TRIGGER_WINDOW = 6
ALTERNATION_BREAK_PATTERNS = {"010101", "101010"}

# 固定数据规律：检测到特定 6 位序列后，按照规律下注
FIXED_PATTERN_TRIGGER_WINDOW = 6
FIXED_PATTERNS = {
    "010101": {"follow": "reverse", "label": "交替循环反转"},  # 按最新一手反向下注
    "101010": {"follow": "reverse", "label": "交替循环反转"},  # 按最新一手反向下注
    "111111": {"follow": "1", "label": "大龙延续"},
    "000000": {"follow": "0", "label": "小龙延续"},
}

# 同手位防卡死：避免 SKIP/超时导致长期不落单
STALL_GUARD_SKIP_MAX = 2
STALL_GUARD_TIMEOUT_MAX = 2
STALL_GUARD_TOTAL_MAX = 6
STALL_GUARD_LOW_STEP_UNLOCK_MAX = 2
STALL_GUARD_HIGH_STEP_MIN = 3
STALL_GUARD_HIGH_STEP_PAUSE_ROUNDS = 1

MODEL_FALLBACK_PAUSE_THRESHOLD = 5
MODEL_FALLBACK_PAUSE_ROUNDS = 2
COUNTED_MODEL_FALLBACK_SOURCES = {
    "timeout_fallback",
    "invalid_fallback",
    "hard_fallback",
    "fallback",
    "fallback_skip",
    "timeout_wait",
    "invalid_wait",
    "hard_wait",
    "model_wait",
}
MODEL_PROBE_INTERVAL_SECONDS = 3
MODEL_PROBE_FAILURE_NOTIFY_INTERVAL_SECONDS = 60

def log_event(level, module, event, message=None, **kwargs):
    # 兼容旧调用: log_event(level, event, message, user_id, data)
    if message is None:
        message = event
        event = module
        module = 'zq'
    category = str(kwargs.pop("category", "")).strip().lower()
    account_name = str(kwargs.pop("account_name", "")).strip()
    user_id = kwargs.get('user_id', 0)
    user_id_text = str(user_id)
    account_slug = str(kwargs.pop("account_slug", "")).strip()
    if not account_slug:
        account_slug = _ACCOUNT_SLUG_REGISTRY.get(user_id_text, "")
    if not account_slug:
        account_slug = _sanitize_account_slug(account_name, fallback=(f"user-{user_id_text}" if user_id_text not in {"", "0"} else "unknown"))
    if category not in {"runtime", "warning", "business"}:
        category = _infer_log_category(level, str(module), str(event))
    data = ', '.join(f'{k}={v}' for k, v in kwargs.items())
    # 使用 'mod' 而不是 'module'，因为 'module' 是 logging 的保留字段
    logger.log(
        level,
        message,
        extra={
            'user_id': user_id_text,
            'mod': module,
            'event': event,
            'data': data,
            'category': category,
            'account_slug': account_slug,
            'account_tag': f"【ydx-{account_slug}】",
        },
    )


# 格式化数字
def format_number(num):
    """与 master 版一致：使用千分位格式。"""
    return f"{int(num):,}"


def heal_stale_pending_bets(user_ctx: UserContext) -> Dict[str, Any]:
    """
    启动时自愈历史挂单：
    - 仅允许“最后一笔且 runtime.bet=True”保持 result=None（真实待结算）
    - 其他 result=None 一律标记为“异常未结算”，避免历史统计与资金核对长期受污染
    """
    state = user_ctx.state
    rt = state.runtime
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    if not logs:
        return {"count": 0, "items": []}

    pending_active = bool(rt.get("bet", False))
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    healed_items: List[str] = []

    for idx, item in enumerate(logs):
        if not isinstance(item, dict):
            continue
        if item.get("result") is not None:
            continue

        is_last = (idx == len(logs) - 1)
        if is_last and pending_active:
            # 正常待结算，不处理
            continue

        item["result"] = "异常未结算"
        if item.get("profit") is None:
            item["profit"] = 0
        item["heal_time"] = now_text
        item["heal_note"] = "startup_auto_heal_pending_bet"
        healed_items.append(str(item.get("bet_id") or f"index:{idx}"))

    healed_count = len(healed_items)
    if healed_count > 0:
        rt["pending_bet_heal_total"] = int(rt.get("pending_bet_heal_total", 0) or 0) + healed_count
        rt["pending_bet_last_heal_count"] = healed_count
        rt["pending_bet_last_heal_at"] = now_text

    return {"count": healed_count, "items": healed_items}


def _get_strategy_bet_sequence_log(state: UserState) -> List[Dict[str, Any]]:
    """Return the bet log slice that belongs to the current betting strategy chain."""
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    rt = state.runtime if isinstance(getattr(state, "runtime", None), dict) else {}
    try:
        reset_index = int(rt.get("bet_reset_log_index", 0) or 0)
    except (TypeError, ValueError):
        reset_index = 0
    reset_index = max(0, min(reset_index, len(logs)))
    if reset_index <= 0:
        return logs
    if reset_index >= len(logs):
        return []
    return logs[reset_index:]


def _get_latest_open_bet_entry(state: UserState) -> Optional[Dict[str, Any]]:
    """返回最新一条未结算押注，供重复下注保护和结算对位使用。"""
    logs = _get_strategy_bet_sequence_log(state)
    for item in reversed(logs):
        if isinstance(item, dict) and item.get("result") is None:
            return item
    return None


def _collect_effective_bet_chain(state: UserState, include_open: bool = False) -> List[Dict[str, Any]]:
    """
    取出“上一笔赢单之后到当前”的真实下注链。
    - 忽略“异常未结算”等脏记录
    - 可选把最后一条未结算押注也算进当前链路
    """
    logs = _get_strategy_bet_sequence_log(state)
    effective_logs: List[Dict[str, Any]] = []
    open_included = False

    for item in logs:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if result == "异常未结算":
            continue
        if result is None:
            if include_open and not open_included:
                effective_logs.append(item)
                open_included = True
            continue
        effective_logs.append(item)

    chain: List[Dict[str, Any]] = []
    for item in reversed(effective_logs):
        chain.append(item)
        if item.get("result") == "赢" and len(chain) > 1:
            chain.pop()
            break
        if item.get("result") == "赢":
            chain = []
            break
    chain.reverse()
    return chain


def _summarize_effective_bet_chain(state: UserState, include_open: bool = False) -> Dict[str, Any]:
    """根据真实下注链回算连续押注、连输和下一手基准，避免幽灵挂单污染 runtime。"""
    chain = _collect_effective_bet_chain(state, include_open=include_open)
    continuous_count = len(chain)
    lose_count = sum(1 for item in chain if item.get("result") == "输")
    total_losses = sum(
        abs(int(item.get("profit", 0) or 0))
        for item in chain
        if int(item.get("profit", 0) or 0) < 0
    )

    last_amount = 0
    if chain:
        try:
            last_amount = int(chain[-1].get("amount", 0) or 0)
        except Exception:
            last_amount = 0

    start_round = "?"
    start_seq = "?"
    if chain:
        first_bet_id = str(chain[0].get("bet_id", "") or "")
        try:
            if "_" in first_bet_id:
                _, parsed_round, parsed_seq = first_bet_id.split("_")
                start_round, start_seq = parsed_round, parsed_seq
            else:
                nums = re.findall(r"\d+", first_bet_id)
                if len(nums) >= 4:
                    start_round, start_seq = nums[-2], nums[-1]
                else:
                    start_round = chain[0].get("round", "?")
                    start_seq = chain[0].get("sequence", "?")
        except Exception:
            start_round = chain[0].get("round", "?")
            start_seq = chain[0].get("sequence", "?")

    return {
        "chain": chain,
        "continuous_count": continuous_count,
        "lose_count": lose_count,
        "total_losses": total_losses,
        "last_amount": last_amount,
        "start_round": start_round,
        "start_seq": start_seq,
    }


def _summarize_recent_resolved_chain(state: UserState) -> Dict[str, Any]:
    """
    返回“以最近一次真实结算为结尾”的链路。
    例如：输输输赢 -> 返回 4 手；输输输 -> 返回 3 手。
    """
    logs = _get_strategy_bet_sequence_log(state)
    effective_logs: List[Dict[str, Any]] = []
    for item in logs:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if result in (None, "异常未结算"):
            continue
        effective_logs.append(item)

    if not effective_logs:
        chain: List[Dict[str, Any]] = []
    else:
        chain = [effective_logs[-1]]
        for item in reversed(effective_logs[:-1]):
            if item.get("result") == "赢":
                break
            chain.append(item)
        chain.reverse()

    total_losses = sum(
        abs(int(item.get("profit", 0) or 0))
        for item in chain
        if int(item.get("profit", 0) or 0) < 0
    )
    lose_count = sum(1 for item in chain if item.get("result") == "输")

    return {
        "chain": chain,
        "continuous_count": len(chain),
        "lose_count": lose_count,
        "total_losses": total_losses,
    }


def reconcile_bet_runtime_from_log(user_ctx: UserContext, include_open: bool = False) -> Dict[str, Any]:
    """
    用真实下注链回写 runtime。
    这一步专门兜底“重复触发下注导致 sequence 脏掉”的情况。
    """
    state = user_ctx.state
    rt = state.runtime
    summary = _summarize_effective_bet_chain(state, include_open=include_open)
    initial_amount = int(rt.get("initial_amount", 500) or 500)

    rt["bet_sequence_count"] = int(summary["continuous_count"])
    rt["lose_count"] = int(summary["lose_count"])
    rt["bet_amount"] = int(summary["last_amount"] or initial_amount) if summary["continuous_count"] > 0 else initial_amount
    return summary


def _append_bet_sequence_entry(state: UserState, entry: Dict[str, Any]) -> None:
    logs = state.bet_sequence_log if isinstance(state.bet_sequence_log, list) else []
    logs.append(entry)
    state.bet_sequence_log = trim_bet_sequence_log(logs, state.runtime)


def _extract_history_from_bet_on_text(text: str) -> List[int]:
    history_match = re.search(r"\[0\s*小\s*1\s*大\]([\s\S]*)", str(text or ""))
    if not history_match:
        return []
    history_str = history_match.group(1)
    return [int(x) for x in re.findall(r"(?<!\d)[01](?!\d)", history_str)]


def _infer_history_advance_result(history_before: List[int], incoming_history: List[int]) -> Dict[str, Any]:
    if not history_before or not incoming_history:
        return {"advanced": False, "result": None, "shift": 0, "mode": ""}
    if len(incoming_history) < len(history_before):
        return {"advanced": False, "result": None, "shift": 0, "mode": "insufficient_window"}

    old = list(history_before)
    new = list(incoming_history)

    # 兼容“由近及远”和“由远及近”两种潜在排列，只要能识别出单步推进就直接推断最新结果。
    max_shift = min(3, len(old), len(new))
    for shift in range(1, max_shift + 1):
        if len(new) == len(old) and new[shift:] == old[:-shift]:
            return {"advanced": True, "result": int(new[shift - 1]), "shift": shift, "mode": "near_to_far"}
        if len(new) == len(old) and new[:-shift] == old[shift:]:
            return {"advanced": True, "result": int(new[-shift]), "shift": shift, "mode": "chronological"}
        if len(new) == len(old) + shift and new[shift:] == old:
            return {"advanced": True, "result": int(new[shift - 1]), "shift": shift, "mode": "near_to_far_extend"}
        if len(new) == len(old) + shift and new[:-shift] == old:
            return {"advanced": True, "result": int(new[-shift]), "shift": shift, "mode": "chronological_extend"}

    return {"advanced": bool(new != old), "result": None, "shift": 0, "mode": "unknown"}


def _heal_runtime_open_bet(open_bet_entry: Dict[str, Any], rt: Dict[str, Any]) -> str:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open_bet_entry["result"] = "异常未结算"
    if open_bet_entry.get("profit") is None:
        open_bet_entry["profit"] = 0
    open_bet_entry["heal_time"] = now_text
    open_bet_entry["heal_note"] = "runtime_auto_heal_missed_settle"
    rt["bet"] = False
    rt["pending_bet_heal_total"] = int(rt.get("pending_bet_heal_total", 0) or 0) + 1
    rt["pending_bet_last_heal_count"] = 1
    rt["pending_bet_last_heal_at"] = now_text
    return str(open_bet_entry.get("bet_id") or "unknown")


def _apply_inferred_settle_from_history(state: UserState, rt: Dict[str, Any], open_bet_entry: Dict[str, Any], inferred_result: int) -> Dict[str, Any]:
    prediction = int(rt.get("bet_type", -1))
    bet_amount = int(open_bet_entry.get("amount", rt.get("bet_amount", 500)) or rt.get("bet_amount", 500) or 500)
    win = (int(inferred_result) == 1 and prediction == 1) or (int(inferred_result) == 0 and prediction == 0)
    result_text = "赢" if win else "输"
    profit = int(bet_amount * 0.99) if win else -bet_amount
    old_lose_count = int(rt.get("lose_count", 0) or 0)

    rt["bet"] = False
    state.bet_type_history.append(prediction)
    rt["gambling_fund"] = int(rt.get("gambling_fund", 0) or 0) + profit
    rt["earnings"] = int(rt.get("earnings", 0) or 0) + profit
    rt["period_profit"] = int(rt.get("period_profit", 0) or 0) + profit
    rt["win_total"] = int(rt.get("win_total", 0) or 0) + (1 if win else 0)
    rt["win_count"] = int(rt.get("win_count", 0) or 0) + 1 if win else 0
    rt["lose_count"] = int(rt.get("lose_count", 0) or 0) + 1 if not win else 0
    rt["status"] = 1 if win else 0

    open_bet_entry["result"] = result_text
    open_bet_entry["profit"] = profit
    open_bet_entry["settled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open_bet_entry["settle_note"] = "inferred_from_history_advance"

    active_chain_summary = _summarize_effective_bet_chain(state)
    if not win:
        rt["bet_sequence_count"] = max(
            int(active_chain_summary.get("continuous_count", 0)),
            old_lose_count + 1,
        )
        rt["lose_count"] = max(
            int(active_chain_summary.get("lose_count", 0)),
            old_lose_count + 1,
        )
        rt["bet_amount"] = int(active_chain_summary.get("last_amount", bet_amount) or bet_amount)
    if win or rt.get("lose_count", 0) >= rt.get("lose_stop", 13):
        rt["bet_sequence_count"] = 0
        rt["bet_amount"] = int(rt.get("initial_amount", 500))

    return {
        "win": win,
        "result_text": result_text,
        "profit": profit,
        "bet_amount": bet_amount,
        "sequence_after": int(rt.get("bet_sequence_count", 0) or 0),
        "lose_count_after": int(rt.get("lose_count", 0) or 0),
        "next_bet_amount": int(calculate_bet_amount(rt) or 0),
    }


def _build_runtime_chain_diag(rt: Dict[str, Any], state: Optional[UserState] = None, **extra: Any) -> Dict[str, Any]:
    diag: Dict[str, Any] = {
        "bet_flag": bool(rt.get("bet", False)),
        "bet_on": bool(rt.get("bet_on", False)),
        "mode_stop": bool(rt.get("mode_stop", False)),
        "manual_pause": bool(rt.get("manual_pause", False)),
        "bet_sequence_count": int(rt.get("bet_sequence_count", 0) or 0),
        "lose_count": int(rt.get("lose_count", 0) or 0),
        "bet_amount": int(rt.get("bet_amount", 0) or 0),
        "stop_count": int(rt.get("stop_count", 0) or 0),
        "current_round": int(rt.get("current_round", 0) or 0),
        "current_bet_seq": int(rt.get("current_bet_seq", 0) or 0),
        "last_settle_message_id": int(rt.get("last_settle_message_id", 0) or 0),
    }
    if state is not None:
        try:
            diag["history_len"] = len(state.history)
        except Exception:
            pass
        try:
            diag["bet_log_len"] = len(state.bet_sequence_log)
        except Exception:
            pass
        open_entry = _get_latest_open_bet_entry(state)
        if open_entry:
            diag["open_bet_id"] = str(open_entry.get("bet_id", "unknown"))
            diag["open_bet_amount"] = int(open_entry.get("amount", 0) or 0)
    for key, value in extra.items():
        if value is None:
            continue
        diag[key] = value
    return diag


def build_pending_bet_heal_notice(healed_pending: Dict[str, Any], summary: Dict[str, Any], rt: Dict[str, Any]) -> str:
    """生成历史脏挂单自愈提示，便于管理员快速确认当前已对齐到哪一手。"""
    healed_count = int(healed_pending.get("count", 0) or 0)
    if healed_count <= 0:
        return ""

    continuous_count = int(summary.get("continuous_count", 0) or 0)
    lose_count = int(summary.get("lose_count", 0) or 0)
    healed_items = healed_pending.get("items", []) if isinstance(healed_pending.get("items"), list) else []

    try:
        next_bet_amount = int(calculate_bet_amount(rt))
    except Exception:
        next_bet_amount = int(rt.get("initial_amount", 0) or 0)

    fixed_text = "、".join(str(item) for item in healed_items[:3]) if healed_items else "已自动修正"
    if len(healed_items) > 3:
        fixed_text += " 等"

    return _build_ops_card(
        "🩹 已修正历史异常挂单",
        summary="检测到历史挂单与当前运行态不一致，系统已自动对齐。",
        fields=[
            ("修复条数", healed_count),
            ("修复记录", fixed_text),
            ("当前连续押注", f"{continuous_count} 次"),
            ("当前连输", f"{lose_count} 次"),
            ("下一手预计下注", _format_money_message(next_bet_amount)),
        ],
        action="建议先执行 `status` 确认当前状态，无需手动重启。",
        note="已按真实已结算记录重新对齐状态。",
    )


def _normalize_ai_keys(ai_cfg: Dict[str, Any]) -> List[str]:
    """统一读取 ai api_keys，兼容旧字段 api_key。"""
    if not isinstance(ai_cfg, dict):
        return []
    raw = ai_cfg.get("api_keys", ai_cfg.get("api_key", []))
    if isinstance(raw, str):
        key = raw.strip()
        return [key] if key else []
    if isinstance(raw, list):
        keys: List[str] = []
        for item in raw:
            text = str(item).strip()
            if text:
                keys.append(text)
        return keys
    return []


def _mask_api_key(key: str) -> str:
    text = str(key or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"


def _looks_like_ai_key_issue(error_text: str) -> bool:
    text = str(error_text or "").lower()
    if not text:
        return False

    # 明确排除非鉴权问题，避免误判。
    non_auth_signals = ("rate limit", "429", "timeout", "connection", "network")
    if any(sig in text for sig in non_auth_signals):
        return False

    auth_signals = (
        "401",
        "unauthorized",
        "authentication",
        "invalid api key",
        "api key is invalid",
        "invalid token",
        "bad api key",
        "incorrect api key",
        "expired",
        "forbidden",
    )
    return any(sig in text for sig in auth_signals)


def _mark_ai_key_issue(rt: Dict[str, Any], reason: str):
    rt["ai_key_issue_active"] = True
    rt["ai_key_issue_reason"] = str(reason or "")[:200]


def _clear_ai_key_issue(rt: Dict[str, Any]):
    rt["ai_key_issue_active"] = False
    rt["ai_key_issue_reason"] = ""
    rt["last_ai_key_warning_notice_sig"] = ""


def _build_ai_key_warning_message(rt: Dict[str, Any]) -> str:
    reason = str(rt.get("ai_key_issue_reason", "")).strip()
    return _build_ops_card(
        "🔑 模型鉴权异常提醒",
        summary="当前模型鉴权异常，系统可能无法稳定调用模型。",
        fields=[
            ("当前模型", rt.get("current_model_id", "unknown")),
            ("原因", reason or "未返回明确鉴权原因"),
        ],
        action="建议在管理员窗口执行 `apikey` 或 `models` 检查当前配置，并准备新的可用 key。",
        note="若后续整条模型链都不可用，系统会继续发出兜底告警。",
    )


def _summarize_model_error(error_text: str, max_parts: int = 2) -> str:
    text = str(error_text or "").strip()
    if not text:
        return "未返回明确错误"

    text = re.sub(r"^Model Error:\s*", "", text)
    replacements = [
        ("NVIDIA API Error", "NVIDIA 接口错误"),
        ("NVIDIA Request Error", "NVIDIA 请求异常"),
        ("NVIDIA API Timeout", "NVIDIA 接口超时"),
        ("OpenAI Compatible API Error", "兼容接口错误"),
        ("OpenAI Compatible Request Error", "兼容接口请求异常"),
        ("OpenAI Compatible API Timeout", "兼容接口超时"),
        ("iFlow API Error", "iFlow 接口错误"),
        ("iFlow Request Error", "iFlow 请求异常"),
        ("iFlow API Timeout", "iFlow 接口超时"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)

    parts = [part.strip() for part in text.split(" | ") if part.strip()]
    if not parts:
        parts = [text]

    picked: List[str] = []
    for part in parts[:max_parts]:
        cleaned = re.sub(r"^[^:]{1,120}? 调用失败:\s*", "", part).strip()
        picked.append(cleaned[:80])

    if len(parts) > max_parts:
        picked.append(f"等 {len(parts)} 项")
    return "；".join(piece for piece in picked if piece)[:180]


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_event_time_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).strftime("%H:%M:%S")
        except Exception:
            continue
    return raw[-8:] if len(raw) >= 8 else raw


def _mark_model_success(rt: Dict[str, Any], model_id: str, switched_from: str = "") -> None:
    rt["model_last_ok_at"] = _now_text()
    rt["model_fallback_streak"] = 0
    rt["model_pause_notified"] = False
    rt["model_probe_current_target"] = ""
    rt["model_probe_total"] = 0
    rt["model_probe_position"] = 0
    rt["model_probe_round_failures"] = 0
    rt["model_probe_last_notify_at"] = 0
    if switched_from and switched_from != model_id:
        rt["model_health_status"] = "switched"
        rt["model_last_switch_from"] = switched_from
        rt["model_last_switch_to"] = model_id
    else:
        rt["model_health_status"] = "ok"
        rt["model_last_switch_from"] = ""
        rt["model_last_switch_to"] = ""


def _mark_model_failure(rt: Dict[str, Any], source: str, reason: str) -> int:
    rt["model_last_fail_at"] = _now_text()
    rt["model_last_fail_reason"] = _summarize_model_error(reason)
    if source in COUNTED_MODEL_FALLBACK_SOURCES:
        rt["model_fallback_streak"] = int(rt.get("model_fallback_streak", 0) or 0) + 1
        rt["model_health_status"] = "down" if int(rt["model_fallback_streak"]) >= MODEL_FALLBACK_PAUSE_THRESHOLD else "fallback"
    else:
        rt["model_fallback_streak"] = 0
        rt["model_health_status"] = "warning"
    return int(rt.get("model_fallback_streak", 0) or 0)


def _build_model_health_lines(rt: Dict[str, Any]) -> List[str]:
    status = str(rt.get("model_health_status", "unknown") or "unknown").strip().lower()
    fallback_streak = int(rt.get("model_fallback_streak", 0) or 0)
    stat_fallback_enabled = bool(rt.get("stat_fallback_bet_enabled", True))
    streak_label = "连续兜底" if stat_fallback_enabled else "连续异常"
    current_model = str(rt.get("current_model_id", "unknown") or "unknown")
    last_fail_reason = str(rt.get("model_last_fail_reason", "") or "").strip()
    last_ok_at = _format_event_time_text(rt.get("model_last_ok_at", ""))
    switch_from = str(rt.get("model_last_switch_from", "") or "").strip()
    probe_active = bool(rt.get("model_probe_active", False))
    probe_target = str(rt.get("model_probe_current_target", "") or "").strip()
    probe_total = int(rt.get("model_probe_total", 0) or 0)
    probe_position = int(rt.get("model_probe_position", 0) or 0)

    if probe_active:
        lines = ["🤖 模型状态：🟡 恢复探测中"]
    elif status == "ok":
        lines = ["🤖 模型状态：🟢 正常"]
    elif status == "switched":
        lines = ["🤖 模型状态：🟡 已切换"]
    elif status == "recovered":
        lines = ["🤖 模型状态：🟢 已恢复"]
    elif status == "fallback":
        lines = [f"🤖 模型状态：🟠 {streak_label} {fallback_streak} 次"]
    elif status == "down":
        lines = ["🤖 模型状态：🔴 不可用（已自动暂停）"]
    else:
        lines = ["🤖 模型状态：⚪ 未知"]

    lines.append(f"当前模型：{current_model}")
    if probe_active and probe_target:
        lines.append(f"当前尝试：{probe_target}")
        if probe_total > 0 and probe_position > 0:
            lines.append(f"探测进度：{probe_position} / {probe_total}")
        lines.append(f"下次重试：约 {MODEL_PROBE_INTERVAL_SECONDS} 秒后")
    if status == "switched" and switch_from:
        lines.append(f"最近异常：{switch_from} 不可用")
    elif status in {"fallback", "down", "warning"} and last_fail_reason:
        lines.append(f"最近异常：{last_fail_reason}")
    if last_ok_at:
        lines.append(f"最近成功：{last_ok_at}")
    return lines


def _build_strategy_watch_line(rt: Dict[str, Any]) -> str:
    pause_reason = str(rt.get("pause_countdown_reason", "") or "").strip()
    if "连续观望暂停" in pause_reason and bool(rt.get("pause_countdown_active", False)):
        return f"策略观望：{pause_reason}"

    skip_streak = int(rt.get("stall_guard_skip_streak", 0) or 0)
    sequence = int(rt.get("stall_guard_sequence", -1) or -1)
    if skip_streak > 0 and sequence > 0:
        return f"策略观望：当前手位连续观望 {skip_streak} 次"
    return ""


def _build_lose_warning_lines(rt: Dict[str, Any]) -> List[str]:
    lose_count = int(rt.get("lose_count", 0) or 0)
    warning_lose_count = int(rt.get("warning_lose_count", 3) or 3)
    if warning_lose_count <= 0 or lose_count < warning_lose_count:
        return []
    lights = " ".join(["🟡"] * max(1, lose_count))
    return [f"连输：{lights}"]


def _get_model_probe_ids(user_ctx: UserContext) -> List[str]:
    model_mgr = user_ctx.get_model_manager()
    ids: List[str] = []
    if model_mgr.fallback_chain:
        for item in model_mgr.fallback_chain:
            cfg = model_mgr.get_model(str(item))
            model_id = str(cfg.get("model_id", "") or "").strip() if cfg else ""
            if model_id and model_id not in ids:
                ids.append(model_id)
    if not ids:
        for cfg in model_mgr.models:
            model_id = str(cfg.get("model_id", "") or "").strip()
            if model_id and model_id not in ids and cfg.get("enabled", True):
                ids.append(model_id)
    return ids


def _is_stat_fallback_bet_enabled(user_ctx: UserContext) -> bool:
    rt = user_ctx.state.runtime if isinstance(getattr(user_ctx, "state", None), UserState) else {}
    if isinstance(rt, dict) and "stat_fallback_bet_enabled" in rt:
        return bool(rt.get("stat_fallback_bet_enabled", True))
    ai_cfg = user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {}
    return bool(ai_cfg.get("enable_stat_fallback_bet", True))


MODEL_WAIT_SOURCES = {"model_wait", "timeout_wait", "invalid_wait", "hard_wait"}


async def _probe_single_model(user_ctx: UserContext, model_id: str) -> Dict[str, Any]:
    model_mgr = user_ctx.get_model_manager()
    model_cfg = model_mgr.get_model(model_id)
    if not model_cfg:
        for cfg in model_mgr.models:
            if str(cfg.get("model_id", "") or "") == str(model_id):
                model_cfg = cfg
                break
    if not model_cfg:
        return {"success": False, "model_id": model_id, "error": "模型不存在"}

    messages = [{"role": "user", "content": "只回复 OK"}]
    provider = str(model_cfg.get("provider", "") or "").strip().lower()
    try:
        if provider == "aliyun":
            result = await model_mgr._call_aliyun(model_cfg, messages, temperature=0.0, max_tokens=16)
        elif provider == "google":
            result = await model_mgr._call_google(model_cfg, messages, temperature=0.0, max_tokens=16)
        else:
            result = await model_mgr._call_iflow(model_cfg, messages, temperature=0.0, max_tokens=16)
    except Exception as e:
        return {"success": False, "model_id": model_id, "error": str(e)}

    result["model_id"] = str(model_cfg.get("model_id", model_id) or model_id)
    return result


def _should_notify_model_probe_failure(rt: Dict[str, Any]) -> bool:
    last_notify_at = int(rt.get("model_probe_last_notify_at", 0) or 0)
    now_ts = int(datetime.now().timestamp())
    return last_notify_at <= 0 or (now_ts - last_notify_at) >= MODEL_PROBE_FAILURE_NOTIFY_INTERVAL_SECONDS


async def _refresh_model_probe_notice(client, user_ctx: UserContext, global_config: dict) -> None:
    rt = user_ctx.state.runtime
    target = str(rt.get("model_probe_current_target", "") or "").strip() or "unknown"
    total = int(rt.get("model_probe_total", 0) or 0)
    position = int(rt.get("model_probe_position", 0) or 0)
    message = _build_ops_card(
        "⏳ 模型恢复探测中",
        summary=f"当前已连续 {int(rt.get('model_fallback_streak', 0) or 0)} 次统计兜底，系统正在后台轮询可用模型。",
        fields=[
            ("当前尝试", target),
            ("探测进度", f"{position} / {total}" if total > 0 and position > 0 else "准备中"),
            ("下次重试", f"约 {MODEL_PROBE_INTERVAL_SECONDS} 秒后"),
        ],
    )
    await _send_transient_admin_notice(
        client,
        user_ctx,
        global_config,
        message,
        ttl_seconds=max(10, MODEL_PROBE_INTERVAL_SECONDS + 6),
        attr_name="model_probe_notice_message",
        msg_type="info",
    )


async def _run_model_probe_loop(client, user_ctx: UserContext, global_config: dict) -> None:
    rt = user_ctx.state.runtime
    try:
        while int(rt.get("model_fallback_streak", 0) or 0) > 0 or bool(rt.get("model_pause_active", False)):
            probe_ids = _get_model_probe_ids(user_ctx)
            if not probe_ids:
                break

            idx = int(rt.get("model_probe_index", 0) or 0) % len(probe_ids)
            target_model_id = probe_ids[idx]
            rt["model_probe_total"] = len(probe_ids)
            rt["model_probe_position"] = idx + 1
            rt["model_probe_current_target"] = target_model_id
            rt["model_probe_index"] = (idx + 1) % len(probe_ids)
            user_ctx.save_state()
            await _refresh_model_probe_notice(client, user_ctx, global_config)

            result = await _probe_single_model(user_ctx, target_model_id)
            if result.get("success"):
                previous_model = str(rt.get("current_model_id", "") or "").strip()
                actual_model_id = str(result.get("model_id", target_model_id) or target_model_id)
                previous_status = str(rt.get("model_health_status", "") or "").strip().lower()
                rt["current_model_id"] = actual_model_id
                _mark_model_success(
                    rt,
                    actual_model_id,
                    switched_from=(previous_model if previous_model and previous_model != actual_model_id else ""),
                )
                if previous_status in {"fallback", "down", "warning"} and (not previous_model or previous_model == actual_model_id):
                    rt["model_health_status"] = "recovered"

                rt["model_probe_active"] = False
                rt["model_last_fail_reason"] = ""
                rt["model_pause_notified"] = False

                if rt.get("model_pause_active", False):
                    rt["model_pause_active"] = False
                    await _clear_pause_countdown_notice(client, user_ctx)
                    rt["stop_count"] = 0
                    rt["bet"] = False
                    rt["bet_on"] = True
                    rt["mode_stop"] = True
                    rt["switch"] = True
                    _queue_model_notice(
                        rt,
                        "resume",
                        signature=f"resume|{previous_model}->{actual_model_id}",
                        from_model=previous_model or actual_model_id,
                        to_model=actual_model_id,
                        detail="后台探测成功，脚本已自动恢复运行。",
                    )
                elif previous_model and previous_model != actual_model_id:
                    _queue_model_notice(
                        rt,
                        "switch",
                        signature=f"probe|{previous_model}->{actual_model_id}",
                        from_model=previous_model,
                        to_model=actual_model_id,
                        detail="后台探测成功，已切换到可用模型。",
                    )
                else:
                    _queue_model_notice(
                        rt,
                        "resume",
                        signature=f"resume|{actual_model_id}|ok",
                        from_model=actual_model_id,
                        to_model=actual_model_id,
                        detail="后台探测成功，模型已恢复。",
                    )

                user_ctx.save_state()
                await _flush_model_runtime_notice(client, user_ctx, global_config)
                return

            rt["model_probe_round_failures"] = int(rt.get("model_probe_round_failures", 0) or 0) + 1
            rt["model_last_fail_at"] = _now_text()
            rt["model_last_fail_reason"] = _summarize_model_error(result.get("error", "探测失败"))
            if int(rt.get("model_probe_round_failures", 0) or 0) >= len(probe_ids):
                rt["model_probe_round_failures"] = 0
                if _should_notify_model_probe_failure(rt):
                    rt["model_probe_last_notify_at"] = int(datetime.now().timestamp())
                    _queue_model_notice(
                        rt,
                        "probe_failed",
                        signature=f"probe_failed|{rt.get('current_model_id', 'unknown')}|{rt.get('model_probe_last_notify_at', 0)}",
                        from_model=str(rt.get("current_model_id", "unknown")),
                        detail=str(rt.get("model_last_fail_reason", "") or "本轮探测全部失败"),
                    )
                    user_ctx.save_state()
                    await _flush_model_runtime_notice(client, user_ctx, global_config)
            user_ctx.save_state()
            await asyncio.sleep(MODEL_PROBE_INTERVAL_SECONDS)
    finally:
        rt["model_probe_active"] = False
        rt["model_probe_current_target"] = ""
        rt["model_probe_total"] = 0
        rt["model_probe_position"] = 0
        rt["model_probe_round_failures"] = 0
        setattr(user_ctx, "_model_probe_task", None)
        user_ctx.save_state()


def _ensure_model_probe_loop(client, user_ctx: UserContext, global_config: dict) -> None:
    rt = user_ctx.state.runtime
    existing = getattr(user_ctx, "_model_probe_task", None)
    if existing and not existing.done():
        return
    rt["model_probe_active"] = True
    user_ctx.save_state()
    user_ctx._model_probe_task = asyncio.create_task(_run_model_probe_loop(client, user_ctx, global_config))


def _queue_model_notice(
    rt: Dict[str, Any],
    notice_type: str,
    *,
    signature: str,
    from_model: str = "",
    to_model: str = "",
    detail: str = "",
) -> None:
    if not signature:
        return

    sig_key = f"last_model_notice_sig_{notice_type}"
    if rt.get(sig_key, "") == signature:
        return

    rt["pending_model_notice"] = {
        "type": str(notice_type or "").strip(),
        "signature": signature,
        "from_model": str(from_model or "").strip(),
        "to_model": str(to_model or "").strip(),
        "detail": str(detail or "").strip(),
    }
    rt[sig_key] = signature


async def _flush_model_runtime_notice(client, user_ctx: UserContext, global_config: dict) -> None:
    rt = user_ctx.state.runtime
    notice = rt.pop("pending_model_notice", None)
    if not isinstance(notice, dict):
        return

    notice_type = str(notice.get("type", "")).strip()
    from_model = str(notice.get("from_model", "")).strip() or "unknown"
    to_model = str(notice.get("to_model", "")).strip() or "unknown"
    detail = str(notice.get("detail", "")).strip() or "未返回明确说明"

    if notice_type == "switch":
        message = _build_ops_card(
            "🤖 模型已自动切换",
            summary="当前主模型不可用，系统已自动切到备用模型并继续运行。",
            fields=[
                ("原模型", from_model),
                ("当前模型", to_model),
                ("原因", detail),
            ],
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            message,
            ttl_seconds=180,
            attr_name="model_switch_notice_message",
            msg_type="model_switch",
        )
        return

    if notice_type == "failure":
        stat_fallback_enabled = bool(rt.get("stat_fallback_bet_enabled", True))
        if stat_fallback_enabled:
            message = _build_ops_card(
                "🚨 模型链不可用，已切统计兜底",
                summary="当前主模型和备用模型都未返回可用结果，本局已改用统计兜底继续运行。",
                fields=[
                    ("连续兜底", f"{int(rt.get('model_fallback_streak', 0) or 0)} 次"),
                    ("当前模型", from_model),
                    ("最近异常", detail),
                ],
            )
        else:
            message = _build_ops_card(
                "🚨 模型链不可用，已等待模型恢复",
                summary="当前主模型和备用模型都未返回可用结果，本局不会再用统计兜底下注。",
                fields=[
                    ("连续异常", f"{int(rt.get('model_fallback_streak', 0) or 0)} 次"),
                    ("当前模型", from_model),
                    ("最近异常", detail),
                ],
            )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            message,
            ttl_seconds=240,
            attr_name="model_failure_notice_message",
            msg_type="model_failure",
        )
        return

    if notice_type == "pause":
        stat_fallback_enabled = bool(rt.get("stat_fallback_bet_enabled", True))
        summary = (
            f"当前已连续 {int(rt.get('model_fallback_streak', 0) or 0)} 次使用统计兜底，系统已自动暂停。"
            if stat_fallback_enabled
            else f"当前已连续 {int(rt.get('model_fallback_streak', 0) or 0)} 次等待模型恢复，系统已自动暂停。"
        )
        message = _build_ops_card(
            "🔴 模型连续异常，已自动暂停",
            summary=summary,
            fields=[
                ("处理结果", f"已自动暂停 {MODEL_FALLBACK_PAUSE_ROUNDS} 局"),
                ("当前模型", from_model),
                ("最近异常", detail),
            ],
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            message,
            ttl_seconds=240,
            attr_name="model_pause_notice_message",
            msg_type="model_pause",
        )
        return

    if notice_type == "probe_failed":
        message = _build_ops_card(
            "🔴 模型链仍不可用",
            summary="当前模型链已完成一整轮探测，仍未找到可用模型，系统会继续暂停并后台重试。",
            fields=[
                ("当前模型", from_model),
                ("最近异常", detail),
            ],
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            message,
            ttl_seconds=240,
            attr_name="model_probe_failed_message",
            msg_type="model_failure",
        )
        return

    if notice_type == "resume":
        message = _build_ops_card(
            "🟢 模型已恢复，脚本已自动继续运行",
            summary="后台探测已确认模型恢复可用，系统已自动恢复运行。",
            fields=[
                ("当前模型", to_model),
                ("说明", detail),
            ],
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            message,
            ttl_seconds=240,
            attr_name="model_resume_notice_message",
            msg_type="model_resume",
        )


async def _notify_ai_key_warning_if_needed(client, user_ctx: UserContext, global_config: dict) -> None:
    rt = user_ctx.state.runtime
    if not rt.get("ai_key_issue_active", False):
        return

    message = _build_ai_key_warning_message(rt)
    if rt.get("last_ai_key_warning_notice_sig", "") == message:
        return

    rt["last_ai_key_warning_notice_sig"] = message
    await _send_transient_admin_notice(
        client,
        user_ctx,
        global_config,
        message,
        ttl_seconds=300,
        attr_name="ai_key_warning_message",
        msg_type="info",
    )


def get_software_version_text() -> str:
    """返回软件版本展示：tag(hash)。"""
    try:
        info = get_current_repo_info()
        short_commit = info.get("short_commit", "") or "unknown"
        tag = info.get("current_tag", "") or info.get("nearest_tag", "")
        if tag:
            return f"{tag}({short_commit})"
        return short_commit
    except Exception:
        return "unknown"


def _get_pending_release_notice(rt: Dict[str, Any]) -> str:
    latest_tag = str(rt.get("release_latest_tag", "") or "").strip()
    if not latest_tag:
        return ""
    current_version = get_software_version_text()
    if latest_tag in current_version:
        return ""
    return f"📦 新版本：{latest_tag}（可更新）"


def _get_pending_release_notice(rt: Dict[str, Any]) -> str:
    latest_tag = str(rt.get("release_latest_tag", "") or "").strip()
    if not latest_tag:
        return ""
    current_version = get_software_version_text()
    if latest_tag and latest_tag in current_version:
        return ""
    return f"📦 新版本：{latest_tag}（可更新）"


# 仪表盘格式化 - 与master版本保持一致
def format_dashboard(user_ctx: UserContext) -> str:
    """生成 HTML 版 status 卡片。"""
    return generate_status_html(_build_status_html_data(user_ctx))


def get_bet_status_text(rt: Dict[str, Any]) -> str:
    """统一押注状态展示。"""
    if rt.get("manual_pause", False):
        return "手动暂停"
    if not rt.get("switch", True):
        return "已关闭"

    pause_active = bool(rt.get("pause_countdown_active", False))
    stop_count = max(0, int(rt.get("stop_count", 0) or 0))
    if pause_active or stop_count > 0:
        total_rounds = max(0, int(rt.get("pause_countdown_total_rounds", 0) or 0))
        last_remaining = int(rt.get("pause_countdown_last_remaining", -1) or -1)
        reason = str(rt.get("pause_countdown_reason", "") or "").strip()

        remaining_rounds = 0
        if total_rounds > 0 and 0 < last_remaining <= total_rounds:
            remaining_rounds = last_remaining
        elif total_rounds > 0 and stop_count > 0:
            # 兼容内部 stop_count=暂停局数+1 的实现细节，展示时尽量贴近“真实剩余局数”。
            if stop_count > total_rounds:
                remaining_rounds = total_rounds
            else:
                remaining_rounds = stop_count
        elif stop_count > 0:
            remaining_rounds = max(0, stop_count - 1)

        if remaining_rounds > 0 and reason:
            return f"自动暂停（剩{remaining_rounds}局，{reason}）"
        if remaining_rounds > 0:
            return f"自动暂停（剩{remaining_rounds}局）"
        if reason:
            return f"自动暂停（{reason}）"
        return "自动暂停"

    if rt.get("bet_on", False):
        return "运行中"
    return "已暂停"


def _format_account_balance_text(rt: Dict[str, Any]) -> str:
    balance_status = str(rt.get("balance_status", "unknown") or "unknown")
    account_balance = int(rt.get("account_balance", 0) or 0)

    if balance_status == "auth_failed":
        return "Cookie 失效"
    if balance_status == "network_error":
        return "网络异常"
    if account_balance <= 0 and balance_status == "unknown":
        return "获取中"
    return format_number(account_balance)


def _resolve_pause_remaining_rounds(rt: Dict[str, Any]) -> int:
    total_rounds = max(0, int(rt.get("pause_countdown_total_rounds", 0) or 0))
    last_remaining = int(rt.get("pause_countdown_last_remaining", -1) or -1)
    stop_count = max(0, int(rt.get("stop_count", 0) or 0))

    if total_rounds > 0 and 0 < last_remaining <= total_rounds:
        return last_remaining
    if total_rounds > 0 and stop_count > 0:
        if stop_count > total_rounds:
            return total_rounds
        return stop_count
    if stop_count > 0:
        return max(0, stop_count - 1)
    return 0


def _resolve_status_html_type(rt: Dict[str, Any]) -> tuple[str, int]:
    if rt.get("manual_pause", False):
        return "manual_pause", 0
    if not rt.get("switch", True):
        return "stop", 0
    if bool(rt.get("pause_countdown_active", False)) or int(rt.get("stop_count", 0) or 0) > 0:
        return "auto_pause", _resolve_pause_remaining_rounds(rt)
    if rt.get("bet_on", False):
        return "running", 0
    return "stop", 0


def _get_current_predict_display(rt: Dict[str, Any]) -> str:
    raw_info = str(rt.get("last_predict_info", "") or "").strip()
    conclusion_match = re.search(r"押注结论：([^\n]+)", raw_info)
    if conclusion_match:
        conclusion = conclusion_match.group(1).strip()
        if "等待模型恢复" in conclusion:
            return "等待恢复"
        if "观望" in conclusion:
            return "观望"
        if "押【大】" in conclusion or "押大" in conclusion:
            return "大"
        if "押【小】" in conclusion or "押小" in conclusion:
            return "小"
    if any(word in raw_info for word in ("观望", "跳过", "SKIP", "skip")):
        return "观望"

    bet_type = rt.get("bet_type", None)
    try:
        if bet_type is not None:
            bet_type = int(bet_type)
            if bet_type == 1:
                return "大"
            if bet_type == 0:
                return "小"
    except (TypeError, ValueError):
        pass

    if "大" in raw_info and "小" not in raw_info:
        return "大"
    if "小" in raw_info and "大" not in raw_info:
        return "小"
    return "等待预测"


def _format_wan_value(value: Any, signed: bool = False) -> str:
    try:
        number = float(value) / 10000.0
    except (TypeError, ValueError):
        number = 0.0
    if signed:
        return f"{number:+.2f}"
    return f"{number:.2f}"


def _format_money_message(value: Any, signed: bool = False) -> str:
    return f"{_format_wan_value(value, signed=signed)} 万"


def _format_total_profit_value(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return f"+{format_number(number)}"
    return format_number(number)


def _build_recent_history_grid(history: List[int], limit: int = 40) -> str:
    recent = history[-limit:][::-1]
    if not recent:
        return "暂无数据"
    icons = ["✅" if x == 1 else "❌" for x in recent]
    return os.linesep.join(
        " ".join(icons[i:i + 10])
        for i in range(0, len(icons), 10)
    )


def _build_status_html_data(user_ctx: UserContext) -> Dict[str, Any]:
    state = user_ctx.state
    rt = state.runtime

    status_type, pause_remain = _resolve_status_html_type(rt)
    total = int(rt.get("total", 0) or 0)
    win_total = int(rt.get("win_total", 0) or 0)
    win_rate = (win_total / total * 100) if total > 0 else 0.0
    next_bet_amount = int(calculate_bet_amount(rt) or 0)

    return {
        "status_type": status_type,
        "pause_remain": pause_remain,
        "preset_name": str(rt.get("current_preset_name", "") or "").strip() or "未设置",
        "version": get_software_version_text(),
        "current_predict": _get_current_predict_display(rt),
        "next_bet": _format_wan_value(next_bet_amount),
        "session_target": _format_wan_value(rt.get("profit", 0)),
        "session_profit": _format_wan_value(rt.get("period_profit", 0), signed=True),
        "acc_bal": _format_wan_value(rt.get("account_balance", 0)),
        "bet_bal": _format_wan_value(rt.get("gambling_fund", 0)),
        "total_profit": _format_wan_value(rt.get("earnings", 0), signed=True),
        "total_profit_raw": _format_total_profit_value(rt.get("earnings", 0)),
        "win_rate": f"{win_rate:.2f}",
        "total_count": total,
        "history_grid": _build_recent_history_grid(state.history),
        "api_model": str(rt.get("current_model_id", "unknown") or "unknown"),
        "ratios": f"{rt.get('lose_once', 3.0)} / {rt.get('lose_twice', 2.1)} / {rt.get('lose_three', 2.05)} / {rt.get('lose_four', 2.0)}",
        "raw_params": f"{rt.get('continuous', 1)} {rt.get('lose_stop', 13)} {rt.get('lose_once', 3.0)} {rt.get('lose_twice', 2.1)} {rt.get('lose_three', 2.05)} {rt.get('lose_four', 2.0)} {rt.get('initial_amount', 500)}",
        "initial_amount": int(rt.get("initial_amount", 500) or 500),
        "lose_stop": int(rt.get("lose_stop", 13) or 13),
        "explode": int(rt.get("explode", 5) or 5),
        "stop": int(rt.get("stop", 3) or 3),
        "account_balance_text": _format_account_balance_text(rt),
        "gambling_fund_text": _format_wan_value(rt.get("gambling_fund", 0)),
        "model_health_lines": _build_model_health_lines({**rt, "stat_fallback_bet_enabled": _is_stat_fallback_bet_enabled(user_ctx)}),
        "lose_warning_lines": _build_lose_warning_lines(rt),
        "strategy_watch_line": _build_strategy_watch_line(rt),
        "release_notice": _get_pending_release_notice(rt),
    }


def generate_status_html(data: Dict[str, Any]) -> str:
    status_map = {
        "running": "🟢 运行中",
        "auto_pause": f"🟡 自动暂停（剩 {int(data.get('pause_remain', 0) or 0)} 局恢复）",
        "manual_pause": "⏸ 手动暂停",
        "stop": "🔴 已停止",
    }
    status_line = status_map.get(str(data.get("status_type", "") or "").strip(), "⚪ 未知状态")

    try:
        current_p = float(data.get("session_profit", 0) or 0)
        target_p = float(data.get("session_target", 0) or 0)
        ratio = max(0.0, min(current_p / target_p, 1.0)) if target_p > 0 else 0.0
        filled = int(10 * ratio)
        progress_bar = f"[{'▓' * filled}{'░' * (10 - filled)}] {ratio * 100:.1f}%"
    except Exception:
        progress_bar = "[░░░░░░░░░░] 0.0%"

    profit_emoji = "📈" if float(data.get("session_profit", 0) or 0) >= 0 else "🚩"
    current_time = datetime.now().strftime("%m-%d %H:%M:%S")

    account_balance_text = str(data.get("account_balance_text", "") or "").strip()
    history_grid = escape_html(str(data.get("history_grid", "暂无数据") or "暂无数据"))
    next_bet = escape_html(str(data.get("next_bet", "0.00") or "0.00"))
    session_target = escape_html(str(data.get("session_target", "0.00") or "0.00"))
    session_profit = escape_html(str(data.get("session_profit", "0.00") or "0.00"))
    total_profit = escape_html(str(data.get("total_profit", "0.00") or "0.00"))
    win_rate = escape_html(str(data.get("win_rate", "0.00") or "0.00"))
    total_count = escape_html(str(data.get("total_count", 0) or 0))
    preset_name = escape_html(str(data.get("preset_name", "未设置") or "未设置"))
    version = escape_html(str(data.get("version", "unknown") or "unknown"))
    current_predict = escape_html(str(data.get("current_predict", "等待预测") or "等待预测"))
    bet_bal = escape_html(str(data.get("bet_bal", "0.00") or "0.00"))
    api_model = escape_html(str(data.get("api_model", "unknown") or "unknown"))
    ratios = escape_html(str(data.get("ratios", "") or ""))
    raw_params = escape_html(str(data.get("raw_params", "") or ""))
    explode = escape_html(str(data.get("explode", 0) or 0))
    stop = escape_html(str(data.get("stop", 0) or 0))
    lose_stop = escape_html(str(data.get("lose_stop", 0) or 0))
    model_health_lines = [escape_html(str(line)) for line in data.get("model_health_lines", []) if str(line).strip()]
    lose_warning_lines = [escape_html(str(line)) for line in data.get("lose_warning_lines", []) if str(line).strip()]
    strategy_watch_line = escape_html(str(data.get("strategy_watch_line", "") or "").strip())
    release_notice = escape_html(str(data.get("release_notice", "") or "").strip())

    if account_balance_text in {"Cookie 失效", "网络异常", "获取中"}:
        account_balance_line = escape_html(account_balance_text)
    else:
        account_balance_line = f"{escape_html(str(data.get('acc_bal', '0.00')))} 万"

    top_warning_block = ""
    if lose_warning_lines:
        top_warning_block = "\n".join(lose_warning_lines) + "\n"

    model_health_block = ""
    if model_health_lines:
        model_health_block = "\n".join(model_health_lines) + "\n\n"
    if release_notice:
        model_health_block = f"{release_notice}\n\n" + model_health_block
    if strategy_watch_line:
        model_health_block += f"{strategy_watch_line}\n\n"

    html = (
        f"{top_warning_block}"
        f"<b>【 状态监控 】</b> {status_line}\n"
        f"<b>更新：</b> {current_time}\n"
        f"<b>版本：</b>{version}\n"
        f"<b>方案：</b> {preset_name}\n\n"
        f"{model_health_block}"

        "<b>🎯 即时下注</b>\n"
        f"├ 下一预测：{current_predict}\n"
        f"├ 计划下注：{next_bet} 万\n"
        f"├ 单轮目标：{session_target} 万\n"
        f"├ 本轮损益：{session_profit} 万 {profit_emoji}\n"
        f"└ 目标进度：{progress_bar}\n\n"

        "<b>💰 资产总览</b>\n"
        f"├ 账户余额：{account_balance_line}\n"
        f"├ 菠菜资金：{bet_bal} 万\n"
        f"├ 累计盈利：{total_profit} 万 🏆\n"
        f"└ 统计数据：{win_rate}% 胜率，{total_count} 次押注\n\n"

        "<b>📊 近期 40 次结果（由近及远）</b>\n"
        "✅：大（1）  ❌：小（0）\n"
        f"{history_grid}\n\n"

        "<b>⚙️ 策略参数</b>\n"
        f"<b>预设名称：</b> {preset_name}\n"
        f"<b>押注倍率：</b> {ratios}\n"
        f"<b>执行规则：</b> 炸 {explode} 停 {stop} | {lose_stop} 次止损\n"
        f"<b>原始参数：</b> {raw_params}"
    )
    return html


def _build_dashboard_summary(user_ctx: UserContext) -> str:
    rt = user_ctx.state.runtime
    status_text = get_bet_status_text(rt)
    preset_name = str(rt.get("current_preset_name", "") or "").strip() or "未设置"
    next_amount = int(calculate_bet_amount(rt) or 0)
    balance_status = rt.get("balance_status", "unknown")
    account_balance = int(rt.get("account_balance", 0) or 0)
    gambling_fund = max(0, int(rt.get("gambling_fund", 0) or 0))

    if balance_status == "auth_failed":
        balance_text = "Cookie 失效"
    elif balance_status == "network_error":
        balance_text = "网络异常"
    elif account_balance <= 0 and balance_status == "unknown":
        balance_text = "获取中"
    else:
        balance_text = f"{account_balance / 10000:.2f} 万"

    next_bet_text = f"{next_amount / 10000:.2f}万" if next_amount > 0 else "已停止"

    summary_lines = [
        "📍 当前概览",
        f"状态：{status_text}",
        f"预设：{preset_name}",
        f"下一手下注：{next_bet_text}",
        f"💰 账户余额：{balance_text}",
        f"💰 菠菜余额：{gambling_fund / 10000:.2f} 万",
        "",
    ]
    return "\n".join(summary_lines)


def _to_bool_switch(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes", "y", "enable", "enabled", "开", "开启"}:
            return True
        if normalized in {"0", "false", "off", "no", "n", "disable", "disabled", "关", "关闭"}:
            return False
    return bool(default)


def _risk_switch_label(enabled: bool) -> str:
    return "ON ✅" if enabled else "OFF ⏸"


def _normalize_risk_switches(rt: Dict[str, Any], apply_default: bool = False) -> Dict[str, bool]:
    """
    统一维护风控“当前开关 + 账号默认开关”。
    apply_default=True 时，会把当前开关重置为账号默认值（用于启动恢复）。
    """
    current_base = _to_bool_switch(rt.get("risk_base_enabled", True), True)
    current_deep = _to_bool_switch(rt.get("risk_deep_enabled", True), True)
    default_base = _to_bool_switch(rt.get("risk_base_default_enabled", current_base), current_base)
    default_deep = _to_bool_switch(rt.get("risk_deep_default_enabled", current_deep), current_deep)

    if apply_default:
        current_base = default_base
        current_deep = default_deep

    rt["risk_base_enabled"] = current_base
    rt["risk_deep_enabled"] = current_deep
    rt["risk_base_default_enabled"] = default_base
    rt["risk_deep_default_enabled"] = default_deep

    return {
        "base_enabled": current_base,
        "deep_enabled": current_deep,
        "base_default_enabled": default_base,
        "deep_default_enabled": default_deep,
    }


def apply_account_risk_default_mode(rt: Dict[str, Any]) -> Dict[str, bool]:
    """启动/重启时应用账号默认风控模式。"""
    return _normalize_risk_switches(rt, apply_default=True)


# 消息分发规则表（与 master 一致）
MESSAGE_ROUTING_TABLE = {
    "win": {"channels": ["admin", "priority"], "priority": True},
    "explode": {"channels": ["admin", "priority"], "priority": True},
    "lose_streak": {"channels": ["admin", "priority"], "priority": True},
    "lose_end": {"channels": ["admin", "priority"], "priority": True},
    "fund_pause": {"channels": ["admin", "priority"], "priority": True},
    "goal_pause": {"channels": ["admin"], "priority": False},
    "model_switch": {"channels": ["admin", "priority"], "priority": True},
    "model_failure": {"channels": ["admin", "priority"], "priority": True},
    "model_pause": {"channels": ["admin", "priority"], "priority": True},
    "model_resume": {"channels": ["admin", "priority"], "priority": True},
    "startup_ready": {"channels": ["admin", "priority"], "priority": True},
    "risk_pause": {"channels": ["admin"], "priority": False},
    "risk_summary": {"channels": ["admin", "priority"], "priority": True},
    "pause": {"channels": ["admin"], "priority": False},
    "resume": {"channels": ["admin"], "priority": False},
    "settle": {"channels": ["admin"], "priority": False},
    "dashboard": {"channels": ["admin"], "priority": False},
    "info": {"channels": ["admin"], "priority": False},
    "warning": {"channels": ["admin"], "priority": False},
    "error": {"channels": ["admin", "priority"], "priority": True},
    "skip_notice": {"channels": ["admin", "priority"], "priority": True},
}

MESSAGE_POLICY = {
    "win": {"level": "P2", "title": "盈利达成", "action": "建议查看 `status`，确认新一轮是否已经开始。"},
    "explode": {"level": "P1", "title": "炸号提醒", "action": "建议立即查看 `status`，必要时调整参数后再继续。"},
    "lose_streak": {"level": "P1", "title": "连输告警", "action": "建议立即查看 `status`，如需止损可执行 `pause`。"},
    "lose_end": {"level": "P2", "title": "连输恢复", "action": "建议关注是否已回到首注，再观察下一次盘口。"},
    "fund_pause": {"level": "P1", "title": "资金暂停", "action": "如需恢复，请执行 `gf 金额` 后再用 `status` 确认。"},
    "goal_pause": {"level": "P1", "title": "目标暂停", "action": "建议等待倒计时结束，或用 `status` 查看剩余暂停局数。"},
    "risk_pause": {"level": "P2", "title": "风控暂停", "action": "建议观察盘面，等待倒计时结束后再继续。"},
    "risk_summary": {"level": "P3", "title": "风控总结", "action": "建议作为复盘信息阅读，不需要立即处理。"},
    "warning": {"level": "P2", "title": "提醒", "action": "建议查看详情，确认是否需要人工介入。"},
    "error": {"level": "P1", "title": "异常提醒", "action": "建议立即查看 `status`，必要时执行 `restart`。"},
}

PRIORITY_FULL_MESSAGE_TYPES = {
    "win",
    "explode",
    "lose_streak",
    "lose_end",
    "fund_pause",
    "goal_pause",
    "model_switch",
    "model_failure",
    "model_pause",
    "model_resume",
    "startup_ready",
    "risk_summary",
    "error",
}


def _strip_account_prefix(text: str) -> str:
    """管理员消息统一移除账号前缀，与 master 行为一致。"""
    if text is None:
        return ""
    raw = str(text)
    normalized = raw.lstrip()
    if not normalized.startswith("【账号："):
        return raw
    lines = normalized.splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[1:]).lstrip("\n")


def _clean_message_lines(text: str) -> List[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _build_ops_card(
    title: str,
    *,
    summary: str = "",
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
) -> str:
    lines = [str(title or "").strip()]
    if summary:
        lines.extend(["", summary])
    for label, value in fields or []:
        if value in (None, ""):
            continue
        label_text = str(label or "").strip()
        value_text = str(value or "").rstrip()
        if not value_text:
            continue
        if not label_text:
            lines.extend(value_text.splitlines())
            continue
        if "\n" in value_text:
            first_line = value_text.splitlines()[0].strip()
            normalized_label = label_text.rstrip("：")
            if first_line.startswith(f"{normalized_label}："):
                lines.extend(value_text.splitlines())
            else:
                lines.append(f"{normalized_label}：")
                lines.extend(value_text.splitlines())
            continue
        lines.append(f"{label_text}：{value_text}")
    if note:
        lines.extend(["", f"补充说明：{note}"])
    return "\n".join(lines).strip()


def _build_alert_ops_card(
    title: str,
    *,
    impact: str,
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
) -> str:
    return _build_ops_card(title, summary=impact, fields=fields, action=action, note=note)


def _build_success_ops_card(
    title: str,
    *,
    outcome: str,
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
) -> str:
    return _build_ops_card(title, summary=outcome, fields=fields, action=action, note=note)


def _build_error_ops_card(
    title: str,
    *,
    problem: str,
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
) -> str:
    return _build_ops_card(title, summary=problem, fields=fields, action=action, note=note)


def _build_help_card() -> str:
    return (
        "<b>📘 脚本命令指南</b>\n\n"
        "结论：常用命令直接点击 <code>代码</code> 即可快速复制。\n"
        "常用：<code>st</code> <code>status</code> <code>pause</code> <code>balance</code>\n\n"
        "<b>⚡ 基础控制（最常用）</b>\n"
        "• <code>/st [预设名]</code> 启动（带名切换并启动）\n"
        "• <code>/status</code> 查看运行状态看板\n"
        "• <code>/pause</code> / <code>/resume</code> 暂停/恢复下注\n"
        "• <code>/balance</code> 刷新当前账户余额\n"
        "• <code>/stats</code> 查看连大、连小、连输统计\n\n"
        "<b>💰 资金与阈值</b>\n"
        "• <code>/gf [金额]</code> 设置菠菜资金上限\n"
        "• <code>/stf [数字]</code> 设置本轮目标金额（单位：万）\n"
        "<i>例：/stf 100</i>\n"
        "• <code>/wlc [n]</code> 连输相关阈值\n\n"
        "<b>🤖 模型与策略（进阶）</b>\n"
        "• <code>/mfb [on/off]</code> 模型链异常时是否继续统计兜底下注\n"
        "• <code>/model list</code> 查看可用模型\n"
        "• <code>/model select [编号/ID]</code> 切换模型（支持编号）\n"
        "<i>例：/model select 1</i>\n"
        "• <code>/apikey show</code> 查看当前密钥状态\n"
        "• <code>/apikey set</code> / <code>/apikey add</code> / <code>/apikey del</code> 管理密钥\n\n"
        "<b>📋 预设与测算（进阶）</b>\n"
        "• <code>/yss</code> 查看全部预设\n"
        "• <code>/yss dl [名]</code> 删除预设\n"
        "• <code>/ys [名称] [连续] [止损] [一输] [二输] [三输] [四输] [首注]</code> 新增或覆盖预设\n"
        "<i>例：/ys 2w 1 10 3.0 2.5 2.2 2.1 20000</i>\n"
        "• <code>/yc [名]</code> 或 <code>/yc [参数...]</code> 按预设或临时参数测算\n\n"
        "<b>🛠 系统与数据（进阶）</b>\n"
        "• <code>/res tj</code> 重置收益/胜率统计\n"
        "• <code>/res state</code> 彻底重置状态\n"
        "• <code>/res bet</code> 只重置当前倍投链路\n"
        "• <code>/ver</code> 查看版本\n"
        "• <code>/restart</code> 重启程序\n"
        "• <code>/update [版本]</code> 更新版本\n"
        "• <code>/reback [版本]</code> 回退版本\n"
        "• <code>/explain</code> 查看最近判断依据\n"
        "• <code>/users</code> 查看当前用户信息\n"
        "• <code>/xx</code> 执行辅助数据操作"
    )


def _build_release_ops_card(
    title: str,
    *,
    summary: str,
    target_version: str = "",
    current_version: str = "",
    restart_required: bool | None = None,
    restart_mode: str = "",
    service_name: str = "",
    restart_command: str = "",
    error: str = "",
    blocking_files: str = "",
    extra_fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
) -> str:
    fields: List[tuple[str, Any]] = []
    if target_version:
        fields.append(("目标版本", target_version))
    if current_version:
        fields.append(("当前版本", current_version))
    if restart_required:
        fields.append(("重启命令", restart_command or "`restart`"))
    if error:
        fields.append(("错误", error))
    if blocking_files:
        fields.append(("阻塞文件", blocking_files))
    for label, value in extra_fields or []:
        if value not in (None, ""):
            fields.append((label, value))
    return _build_ops_card(
        title,
        summary=summary,
        fields=fields,
        action=action,
        note=note,
    )


async def _reply_ops_card(
    event,
    title: str,
    *,
    summary: str = "",
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
):
    return await event.reply(
        _build_ops_card(
            title,
            summary=summary,
            fields=fields,
            action=action,
            note=note,
        )
    )


async def _send_command_ops_card(
    client,
    event,
    user_ctx: UserContext,
    global_config: dict,
    title: str,
    *,
    summary: str = "",
    fields: Optional[List[tuple[str, Any]]] = None,
    action: str = "",
    note: str = "",
    ttl_seconds: int = 60,
):
    sent = await send_to_admin(
        client,
        _build_ops_card(
            title,
            summary=summary,
            fields=fields,
            action=action,
            note=note,
        ),
        user_ctx,
        global_config,
    )
    if sent and ttl_seconds > 0:
        chat_id = getattr(sent, "chat_id", None)
        message_id = getattr(sent, "id", None)
        if chat_id is not None and message_id is not None:
            asyncio.create_task(delete_later(client, chat_id, message_id, ttl_seconds))
    return sent


def _build_priority_summary(msg_type: str, text: str, account_prefix: str) -> str:
    content = _strip_account_prefix(text)
    lines = _clean_message_lines(content)
    policy = MESSAGE_POLICY.get(msg_type, {})
    level = policy.get("level", "P2")
    default_title = policy.get("title", msg_type)
    default_action = policy.get("action", "")

    title = default_title
    summary = ""
    action = ""
    fields: List[tuple[str, str]] = []
    title_prefixes = ("✅", "⚠️", "❌", "📘", "📌", "♻️", "↩️", "🔄", "💰", "🎯", "📚", "👤", "📉", "📊")

    for idx, line in enumerate(lines):
        if (
            idx == 0
            and not any(line.startswith(prefix) for prefix in ("结论：", "建议动作：", "补充说明："))
            and (len(lines) > 1 or line.startswith(title_prefixes))
        ):
            title = line
            continue
        if line.startswith("结论：") and not summary:
            summary = line.removeprefix("结论：").strip()
            continue
        if line.startswith("建议动作：") and not action:
            action = line.removeprefix("建议动作：").strip()
            continue
        if line.startswith("补充说明："):
            continue
        if "：" in line:
            label, value = line.split("：", 1)
            label = label.strip()
            value = value.strip()
            if label and value:
                fields.append((label, value))
            continue
        if not summary:
            summary = line

    if not summary:
        summary = lines[0] if lines else default_title
    if not action:
        action = default_action

    preferred_labels = ["状态", "当前状态", "预设", "下一手下注", "账户余额", "菠菜余额", "目标", "当前", "收益", "损失"]
    picked_fields: List[str] = []
    seen_labels = set()
    for wanted in preferred_labels:
        for label, value in fields:
            if label == wanted and label not in seen_labels:
                picked_fields.append(f"{label}：{value}")
                seen_labels.add(label)
                break
        if len(picked_fields) >= 2:
            break
    if len(picked_fields) < 2:
        for label, value in fields:
            if label in seen_labels:
                continue
            picked_fields.append(f"{label}：{value}")
            seen_labels.add(label)
            if len(picked_fields) >= 2:
                break

    summary_lines: List[str] = [account_prefix, f"[{level}] {title}", summary]
    summary_lines.extend(picked_fields)
    if action:
        summary_lines.append(f"操作：{action}")
    return "\n".join(summary_lines)


def _ensure_account_prefix(text: str, account_prefix: str) -> str:
    """重点渠道消息统一补充账号前缀。"""
    content = _strip_account_prefix(text)
    if not content:
        return account_prefix
    return f"{account_prefix}\n\n{content}"


def _iter_targets(target):
    if isinstance(target, (list, tuple, set)):
        return [item for item in target if item not in (None, "")]
    if target in (None, ""):
        return []
    return [target]


def _get_admin_console_cfg(user_ctx: UserContext) -> Dict[str, Any]:
    return user_ctx.config.admin_console if isinstance(getattr(user_ctx.config, "admin_console", None), dict) else {}


def _get_admin_console_mode(user_ctx: UserContext) -> str:
    return str(_get_admin_console_cfg(user_ctx).get("mode", "") or "").strip()


def _resolve_admin_telegram_id_chat(user_ctx: UserContext):
    cfg = _get_admin_console_cfg(user_ctx).get("telegram_id", {})
    if not isinstance(cfg, dict):
        return None
    target = cfg.get("chat_id")
    if isinstance(target, str):
        text = target.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except Exception:
                return target
        return text
    return target


def _get_admin_telegram_bot_cfg(user_ctx: UserContext) -> Dict[str, Any]:
    cfg = _get_admin_console_cfg(user_ctx).get("telegram_bot", {})
    return cfg if isinstance(cfg, dict) else {}


def _get_notification_channels_cfg(user_ctx: UserContext) -> Dict[str, Any]:
    notification = user_ctx.config.notification if isinstance(user_ctx.config.notification, dict) else {}
    channels = notification.get("channels", {}) if isinstance(notification.get("channels", {}), dict) else {}
    return channels


def _get_notify_channel_cfg(user_ctx: UserContext, channel_name: str) -> Dict[str, Any]:
    channels = _get_notification_channels_cfg(user_ctx)
    cfg = channels.get(channel_name, {})
    return cfg if isinstance(cfg, dict) else {}


def _append_text_record(file_path: str, content: str) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(content)


def _cleanup_daily_interaction_files(root_dir: str, retention_days: int = 7) -> None:
    if retention_days <= 0 or not os.path.isdir(root_dir):
        return
    cutoff = datetime.now().date() - timedelta(days=retention_days - 1)
    for entry in os.scandir(root_dir):
        if not entry.is_file() or not entry.name.endswith((".jsonl", ".log")):
            continue
        try:
            stem = entry.name.rsplit(".", 1)[0]
            file_date = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                os.remove(entry.path)
            except OSError:
                pass


def _mask_command_text(command_text: str) -> tuple[str, bool]:
    text = str(command_text or "").strip()
    if not text:
        return "", False
    parts = text.split()
    if not parts:
        return text, False
    normalized_cmd = parts[0][1:] if parts[0].startswith("/") else parts[0]
    cmd = normalized_cmd.lower()
    if cmd == "apikey" and len(parts) >= 2:
        sub_cmd = parts[1].lower()
        if sub_cmd in {"set", "add"} and len(parts) >= 3:
            return " ".join(parts[:2] + ["***"]), True
    return text, False


def _build_interaction_entry(record: Dict[str, Any]) -> str:
    ts = str(record.get("ts", "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    direction = str(record.get("direction", "") or "").strip().lower()
    kind = str(record.get("kind", "") or "").strip().lower()
    channel = str(record.get("channel", "") or "").strip().lower() or "-"
    text = str(record.get("text", "") or "")

    direction_label = "发送" if direction == "outbound" else "接收" if direction == "inbound" else direction or "未知"
    kind_label = "通知" if kind == "notification" else "命令" if kind == "command" else kind or "事件"

    header_parts = [f"[{ts}]", direction_label, channel, kind_label]
    command = str(record.get("command", "") or "").strip()
    msg_type = str(record.get("msg_type", "") or "").strip()
    if command:
        header_parts.append(command)
    elif msg_type:
        header_parts.append(msg_type)

    if "success" in record:
        header_parts.append("成功" if bool(record.get("success")) else "失败")
    if bool(record.get("masked", False)):
        header_parts.append("已脱敏")
    chat_id = record.get("chat_id", None)
    if chat_id not in (None, ""):
        header_parts.append(f"chat_id={chat_id}")
    error = str(record.get("error", "") or "").strip()
    if error:
        header_parts.append(f"error={error[:160]}")

    header = " | ".join(part for part in header_parts if part)
    body = text if text else "(空内容)"
    separator = "─" * 72
    return f"{header}\n{body}\n{separator}\n\n"


def append_interaction_event(
    user_ctx: UserContext,
    *,
    direction: str,
    kind: str,
    channel: str,
    text: str,
    **extra: Any,
) -> None:
    identity = _resolve_account_identity(user_ctx)
    interaction_dir = os.path.join(ACCOUNT_LOG_ROOT, identity["account_slug"], "interactions")
    file_name = datetime.now().strftime("%Y-%m-%d") + ".log"
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": int(identity["user_id"]) if str(identity["user_id"]).isdigit() else identity["user_id"],
        "account_name": identity["account_name"],
        "account_slug": identity["account_slug"],
        "account_label": identity["account_label"],
        "direction": str(direction or "").strip().lower() or "unknown",
        "kind": str(kind or "").strip().lower() or "unknown",
        "channel": str(channel or "").strip().lower() or "unknown",
        "text": str(text or ""),
    }
    for key, value in extra.items():
        if value is None:
            continue
        record[key] = value
    try:
        _append_text_record(os.path.join(interaction_dir, file_name), _build_interaction_entry(record))
        _cleanup_daily_interaction_files(interaction_dir, retention_days=7)
    except Exception as e:
        log_event(logging.WARNING, "interaction", "写入交互审计失败", user_id=user_ctx.user_id, data=str(e))


def _record_outbound_message(
    user_ctx: UserContext,
    *,
    channel: str,
    text: str,
    msg_type: str,
    success: bool,
    parse_mode: Optional[str] = None,
    title: Optional[str] = None,
    chat_id: Any = None,
    error: Optional[str] = None,
) -> None:
    append_interaction_event(
        user_ctx,
        direction="outbound",
        kind="notification",
        channel=channel,
        text=text,
        msg_type=msg_type,
        success=bool(success),
        parse_mode=parse_mode,
        title=title,
        chat_id=chat_id,
        error=error,
    )


def _normalize_bot_parse_mode(parse_mode: Optional[str]) -> Optional[str]:
    mode = str(parse_mode or "").strip().lower()
    if mode == "html":
        return "HTML"
    return None


def _render_bot_text_payload(text: str, parse_mode: Optional[str]) -> tuple[str, Optional[str]]:
    mode = str(parse_mode or "").strip().lower()
    raw = str(text or "")
    if mode == "html":
        return raw, "HTML"
    if mode != "markdown":
        return raw, None

    placeholders: List[tuple[str, str]] = []

    def _store(value: str) -> str:
        token = f"__BOT_FMT_{len(placeholders)}__"
        placeholders.append((token, value))
        return token

    def _replace_pre(match):
        body = match.group(1)
        return _store(f"<pre>{escape_html(body)}</pre>")

    protected = re.sub(r"```([\s\S]*?)```", _replace_pre, raw)
    escaped = escape_html(protected)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", escaped)

    for token, value in placeholders:
        escaped = escaped.replace(escape_html(token), value)
        escaped = escaped.replace(token, value)
    return escaped, "HTML"


async def _post_form_async(url: str, payload: dict, timeout: int = 5):
    """在异步上下文中安全发送 form 请求，避免阻塞事件循环。"""
    return await asyncio.to_thread(requests.post, url, data=payload, timeout=timeout)


async def _post_json_async(url: str, payload: dict, timeout: int = 5):
    """在异步上下文中安全发送 json 请求，避免阻塞事件循环。"""
    return await asyncio.to_thread(requests.post, url, json=payload, timeout=timeout)


async def send_message_v2(
    client,
    msg_type: str,
    message: str,
    user_ctx: UserContext,
    global_config: dict,
    parse_mode: str = "markdown",
    title=None,
    desp=None
):
    """新版统一消息发送函数（多用户版）- 严格按路由表分发。"""
    routing = MESSAGE_ROUTING_TABLE.get(msg_type)
    if routing is None:
        error = f"未定义消息路由: {msg_type}"
        log_event(logging.ERROR, 'send_msg', '消息路由缺失', user_id=user_ctx.user_id, data=error)
        raise ValueError(error)

    channels = routing.get("channels", [])
    account_name = user_ctx.config.name.strip()
    account_prefix = f"【账号：{account_name}】"
    admin_message = _strip_account_prefix(message)
    priority_source = desp if desp is not None else message
    if msg_type in PRIORITY_FULL_MESSAGE_TYPES:
        priority_message = _ensure_account_prefix(priority_source, account_prefix)
        priority_desp = priority_message
    else:
        priority_message = _build_priority_summary(msg_type, priority_source, account_prefix)
        priority_desp = priority_message

    sent_message = None
    admin_target = None
    if "admin" in channels or "all" in channels:
        try:
            admin_mode = _get_admin_console_mode(user_ctx)
            if admin_mode == "telegram_id":
                admin_target = _resolve_admin_telegram_id_chat(user_ctx)
                if admin_target:
                    sent_message = await client.send_message(admin_target, admin_message, parse_mode=parse_mode)
                    _record_outbound_message(
                        user_ctx,
                        channel="admin_chat",
                        text=admin_message,
                        msg_type=msg_type,
                        success=True,
                        parse_mode=parse_mode,
                        chat_id=admin_target,
                    )
            elif admin_mode == "telegram_bot":
                bot_cfg = _get_admin_telegram_bot_cfg(user_ctx)
                bot_token = str(bot_cfg.get("bot_token", "") or "").strip()
                admin_target = bot_cfg.get("chat_id")
                if bot_token and admin_target not in (None, ""):
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    rendered_text, bot_parse_mode = _render_bot_text_payload(admin_message, parse_mode)
                    payload: Dict[str, Any] = {
                        "chat_id": admin_target,
                        "text": rendered_text,
                    }
                    if bot_parse_mode:
                        payload["parse_mode"] = bot_parse_mode
                    response = await _post_json_async(url, payload, timeout=5)
                    sent_json = response.json() if hasattr(response, "json") else {}
                    result = sent_json.get("result", {}) if isinstance(sent_json, dict) else {}
                    sent_message = SimpleNamespace(
                        chat_id=admin_target,
                        id=result.get("message_id"),
                        is_bot_api=True,
                        bot_token=bot_token,
                    )
                    _record_outbound_message(
                        user_ctx,
                        channel="admin_console.telegram_bot",
                        text=admin_message,
                        msg_type=msg_type,
                        success=True,
                        parse_mode=parse_mode,
                        chat_id=admin_target,
                    )
        except Exception as e:
            log_event(logging.ERROR, 'send_msg', '发送管理员消息失败', user_id=user_ctx.user_id, data=str(e))

    if admin_target is not None and sent_message is None:
        _record_outbound_message(
            user_ctx,
            channel=f"admin_console.{_get_admin_console_mode(user_ctx) or 'unknown'}",
            text=admin_message,
            msg_type=msg_type,
            success=False,
            parse_mode=parse_mode,
            chat_id=admin_target,
            error="send_failed",
        )

    if "priority" in channels or "all" in channels:
        iyuu_cfg = _get_notify_channel_cfg(user_ctx, "iyuu")
        if iyuu_cfg.get("enable"):
            try:
                final_title = title or f"菠菜机器人 {account_name} 通知"
                payload = {"text": final_title, "desp": priority_desp}
                iyuu_url = iyuu_cfg.get("url")
                if not iyuu_url:
                    token = iyuu_cfg.get("token")
                    iyuu_url = f"https://iyuu.cn/{token}.send" if token else None
                if iyuu_url:
                    await _post_form_async(iyuu_url, payload, timeout=5)
                    _record_outbound_message(
                        user_ctx,
                        channel="iyuu",
                        text=priority_desp,
                        msg_type=msg_type,
                        success=True,
                        parse_mode=parse_mode,
                        title=final_title,
                    )
            except Exception as e:
                log_event(logging.ERROR, 'send_msg', 'IYUU通知失败', user_id=user_ctx.user_id, data=str(e))

        tg_bot_cfg = _get_notify_channel_cfg(user_ctx, "telegram_notify_bot")
        if tg_bot_cfg.get("enable"):
            try:
                bot_token = tg_bot_cfg.get("bot_token")
                chat_id = tg_bot_cfg.get("chat_id")
                if bot_token and chat_id:
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    rendered_text, bot_parse_mode = _render_bot_text_payload(priority_message, parse_mode)
                    payload = {"chat_id": chat_id, "text": rendered_text}
                    if bot_parse_mode:
                        payload["parse_mode"] = bot_parse_mode
                    await _post_json_async(url, payload, timeout=5)
                    _record_outbound_message(
                        user_ctx,
                        channel="tg_bot",
                        text=priority_message,
                        msg_type=msg_type,
                        success=True,
                        parse_mode=parse_mode,
                        chat_id=chat_id,
                    )
            except Exception as e:
                log_event(logging.ERROR, 'send_msg', 'TG Bot通知失败', user_id=user_ctx.user_id, data=str(e))

    return sent_message


# 兼容旧接口
async def send_message(
    client,
    to: str,
    message: str,
    user_ctx: UserContext,
    global_config: dict,
    parse_mode: str = "markdown",
    title=None,
    desp=None,
    notify_type: str = "info"
):
    msg_type_map = {
        "profit": "win",
        "explode": "explode",
        "lose_streak": "lose_streak",
        "profit_recovery": "lose_end",
        "skip_notice": "skip_notice",
        "info": "info",
    }
    msg_type = msg_type_map.get(notify_type, "info")
    if to not in ("admin", "all", "priority", "iyuu", "tgbot"):
        log_event(logging.WARNING, 'send_msg', '旧接口to参数无效，已按路由表处理', user_id=user_ctx.user_id, data=f"to={to}, type={msg_type}")
        to = "admin"

    if to == "admin":
        return await send_message_v2(client, "info", message, user_ctx, global_config, parse_mode, title, desp)
    if to == "all":
        return await send_message_v2(client, msg_type, message, user_ctx, global_config, parse_mode, title, desp)

    # priority/iyuu/tgbot 兼容：仅走重点渠道
    account_name = user_ctx.config.name.strip()
    account_prefix = f"【账号：{account_name}】"
    priority_message = _ensure_account_prefix(message, account_prefix)
    priority_desp = _ensure_account_prefix(desp if desp is not None else message, account_prefix)
    if to in ("priority", "iyuu"):
        iyuu_cfg = _get_notify_channel_cfg(user_ctx, "iyuu")
        if iyuu_cfg.get("enable"):
            final_title = title or f"菠菜机器人 {account_name} 通知"
            payload = {"text": final_title, "desp": priority_desp}
            iyuu_url = iyuu_cfg.get("url")
            if not iyuu_url:
                token = iyuu_cfg.get("token")
                iyuu_url = f"https://iyuu.cn/{token}.send" if token else None
            if iyuu_url:
                await _post_form_async(iyuu_url, payload, timeout=5)
                _record_outbound_message(
                    user_ctx,
                    channel="iyuu",
                    text=priority_desp,
                    msg_type=msg_type,
                    success=True,
                    parse_mode=parse_mode,
                    title=final_title,
                )
    if to in ("priority", "tgbot"):
        tg_bot_cfg = _get_notify_channel_cfg(user_ctx, "telegram_notify_bot")
        if tg_bot_cfg.get("enable"):
            bot_token = tg_bot_cfg.get("bot_token")
            chat_id = tg_bot_cfg.get("chat_id")
            if bot_token and chat_id:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {"chat_id": chat_id, "text": priority_message}
                await _post_json_async(url, payload, timeout=5)
                _record_outbound_message(
                    user_ctx,
                    channel="tg_bot",
                    text=priority_message,
                    msg_type=msg_type,
                    success=True,
                    parse_mode=parse_mode,
                    chat_id=chat_id,
                )
    return None


async def send_to_admin(client, message: str, user_ctx: UserContext, global_config: dict):
    return await send_message_v2(client, "info", message, user_ctx, global_config)


async def _send_transient_admin_notice(
    client,
    user_ctx: UserContext,
    global_config: dict,
    message: str,
    ttl_seconds: int = 120,
    attr_name: str = "transient_notice_message",
    msg_type: str = "info",
):
    """
    发送“短时说明通知”（用于暂停结束/恢复等状态提示）：
    - 刷新式保留最后一条
    - 到期自动删除，减少消息堆积
    """
    old_message = getattr(user_ctx, attr_name, None)
    if old_message:
        await cleanup_message(client, old_message)
    if msg_type != "info" and msg_type in MESSAGE_ROUTING_TABLE:
        sent = await send_message_v2(client, msg_type, message, user_ctx, global_config)
    else:
        sent = await send_to_admin(client, message, user_ctx, global_config)
    if sent:
        setattr(user_ctx, attr_name, sent)
        chat_id = getattr(sent, "chat_id", None)
        msg_id = getattr(sent, "id", None)
        if chat_id is not None and msg_id is not None and ttl_seconds > 0:
            asyncio.create_task(delete_later(client, chat_id, msg_id, ttl_seconds))
    return sent


# ==================== 核心预测函数 ====================

def calculate_trend_gap(history, window=100):
    """
    计算趋势缺口：最近N期内"大"和"小"偏离50/50均衡线的数值
    返回: {
        'big_ratio': 大占比,
        'small_ratio': 小占比,
        'deviation_score': 标准差/偏离度,
        'gap': 向均值靠拢的缺口(正=缺大, 负=缺小),
        'regression_target': 统计学理论预测目标(0或1)
    }
    """
    if len(history) < window:
        window = len(history)
    
    recent = history[-window:]
    big_count = sum(recent)
    small_count = window - big_count
    
    big_ratio = big_count / window if window > 0 else 0.5
    small_ratio = small_count / window if window > 0 else 0.5
    
    deviation_score = abs(big_ratio - 0.5) * 2
    
    gap = (window / 2) - big_count
    
    regression_target = 1 if big_count < small_count else 0
    
    return {
        'big_ratio': round(big_ratio, 3),
        'small_ratio': round(small_ratio, 3),
        'deviation_score': round(deviation_score, 3),
        'gap': int(gap),
        'regression_target': regression_target,
        'big_count': big_count,
        'small_count': small_count
    }


def extract_pattern_features(history):
    """
    提取形态特征：自动检测单跳、长龙、对称环绕等状态
    返回: {
        'pattern_tag': 形态标签,
        'tail_streak_len': 尾部连龙长度,
        'tail_streak_char': 尾部连龙字符(0/1),
        'is_alternating': 是否单跳模式,
        'is_symmetric': 是否对称环绕
    }
    """
    if not history or len(history) < 3:
        return {
            'pattern_tag': 'INSUFFICIENT_DATA',
            'tail_streak_len': 0,
            'tail_streak_char': None,
            'is_alternating': False,
            'is_symmetric': False
        }
    
    seq_str = ''.join(['1' if x == 1 else '0' for x in history])
    
    tail_char = seq_str[-1]
    tail_streak_len = 1
    for i in range(len(seq_str) - 2, -1, -1):
        if seq_str[i] == tail_char:
            tail_streak_len += 1
        else:
            break
    
    is_alternating = False
    if len(seq_str) >= 6:
        recent_6 = seq_str[-6:]
        if recent_6 in ['010101', '101010']:
            is_alternating = True
    
    is_symmetric = False
    if len(seq_str) >= 5:
        recent_5 = seq_str[-5:]
        if recent_5 == recent_5[::-1]:
            is_symmetric = True
    
    if tail_streak_len >= 4:
        pattern_tag = 'LONG_DRAGON'
    elif tail_streak_len >= 3:
        pattern_tag = 'DRAGON_CANDIDATE'
    elif tail_streak_len == 2:
        pattern_tag = 'DOUBLE_STREAK'
    elif is_alternating:
        pattern_tag = 'SINGLE_JUMP'
    elif is_symmetric:
        pattern_tag = 'SYMMETRIC_WRAP'
    else:
        pattern_tag = 'CHAOS_SWITCH'
    
    return {
        'pattern_tag': pattern_tag,
        'tail_streak_len': tail_streak_len,
        'tail_streak_char': int(tail_char),
        'is_alternating': is_alternating,
        'is_symmetric': is_symmetric
    }


def analyze_double_streak_followups(history, lookback_events: int = 200):
    """
    统计“刚形成2连之后，下一手是继续还是反转”的条件概率。
    只在 streak 首次达到 2 的那个时点计一次，避免把长连拆成多次重复样本。
    """
    if not history or len(history) < 3:
        return {
            "current_side": "",
            "current_side_total": 0,
            "current_continue": 0,
            "current_reverse": 0,
            "current_continue_rate": 0.0,
            "current_reverse_rate": 0.0,
            "current_preference": "neutral",
        }

    events = []
    streak_len = 1
    for i in range(1, len(history) - 1):
        if history[i] == history[i - 1]:
            streak_len += 1
        else:
            streak_len = 1
        if streak_len == 2:
            side = int(history[i])
            next_value = int(history[i + 1])
            events.append({
                "side": side,
                "continue": next_value == side,
            })

    if lookback_events > 0:
        events = events[-lookback_events:]

    tail_side = int(history[-1])
    current_side_text = "big" if tail_side == 1 else "small"
    side_events = [event for event in events if event["side"] == tail_side]
    total = len(side_events)
    continue_count = sum(1 for event in side_events if event["continue"])
    reverse_count = total - continue_count
    continue_rate = round((continue_count / total), 3) if total > 0 else 0.0
    reverse_rate = round((reverse_count / total), 3) if total > 0 else 0.0

    preference = "neutral"
    if total >= 8:
        if continue_rate - reverse_rate >= 0.08:
            preference = "continue"
        elif reverse_rate - continue_rate >= 0.08:
            preference = "reverse"

    return {
        "current_side": current_side_text,
        "current_side_total": total,
        "current_continue": continue_count,
        "current_reverse": reverse_count,
        "current_continue_rate": continue_rate,
        "current_reverse_rate": reverse_rate,
        "current_preference": preference,
    }


def _best_repeating_pattern_match(seq_str: str, patterns: List[str]) -> Dict[str, Any]:
    """在候选重复节奏里找出最匹配的模板，并推导下一手期望值。"""
    if not seq_str:
        return {"pattern": "", "score": 0.0, "next_char": None}

    best_pattern = ""
    best_score = -1.0
    best_next_char = None
    for pattern in patterns:
        expanded = (pattern * ((len(seq_str) // len(pattern)) + 2))[:len(seq_str)]
        score = sum(1 for current, expected in zip(seq_str, expanded) if current == expected) / len(seq_str)
        if score > best_score:
            best_score = score
            best_pattern = pattern
            best_next_char = pattern[len(seq_str) % len(pattern)]

    return {
        "pattern": best_pattern,
        "score": round(best_score, 3),
        "next_char": int(best_next_char) if best_next_char is not None else None,
    }


def analyze_rhythm_context(history, recent_window: int = 9, lookback_events: int = 200):
    """
    识别当前更像交替节奏、配对节奏、长龙还是混沌，并结合历史窗口统计命中率。
    配对节奏不是简单看 2 连，而是看最近序列更像 101/110/001/010 这类“补成二连”的重复节奏。
    """
    if not history or len(history) < 4:
        return {
            "recent_seq": "",
            "rhythm_tag": "CHAOS_NOISE",
            "alternation_score": 0.0,
            "alternation_pattern": "",
            "alternation_next": None,
            "alternation_hit_rate": 0.0,
            "alternation_samples": 0,
            "pair_score": 0.0,
            "pair_pattern": "",
            "pair_next": None,
            "pair_hit_rate": 0.0,
            "pair_samples": 0,
            "dragon_score": 0.0,
            "chaos_score": 1.0,
            "pair_would_form_double": False,
            "pair_would_chase_triple": False,
        }

    recent_len = min(max(4, int(recent_window)), len(history))
    recent_seq = "".join("1" if x == 1 else "0" for x in history[-recent_len:])

    alternation_patterns = ["01", "10"]
    pair_patterns = ["001", "010", "100", "110", "101", "011"]
    alternation_match = _best_repeating_pattern_match(recent_seq, alternation_patterns)
    pair_match = _best_repeating_pattern_match(recent_seq, pair_patterns)

    tail_streak_len = 1
    tail_value = history[-1]
    for value in reversed(history[:-1]):
        if value == tail_value:
            tail_streak_len += 1
        else:
            break

    dragon_score = round(min(tail_streak_len / 4.0, 1.0), 3) if tail_streak_len >= 2 else 0.0
    pair_would_form_double = tail_streak_len == 1 and pair_match["next_char"] == tail_value
    pair_would_chase_triple = tail_streak_len >= 2 and pair_match["next_char"] == tail_value

    alternation_samples = 0
    alternation_hits = 0
    pair_samples = 0
    pair_hits = 0
    start_idx = max(recent_len, len(history) - max(int(lookback_events), recent_len))
    for idx in range(start_idx, len(history)):
        prior = history[idx - recent_len:idx]
        if len(prior) < recent_len:
            continue
        prior_seq = "".join("1" if x == 1 else "0" for x in prior)
        actual_next = int(history[idx])
        prior_alt = _best_repeating_pattern_match(prior_seq, alternation_patterns)
        prior_pair = _best_repeating_pattern_match(prior_seq, pair_patterns)

        if prior_alt["score"] >= 0.75:
            alternation_samples += 1
            if prior_alt["next_char"] == actual_next:
                alternation_hits += 1
        if prior_pair["score"] >= 0.67:
            pair_samples += 1
            if prior_pair["next_char"] == actual_next:
                pair_hits += 1

    alternation_hit_rate = round(alternation_hits / alternation_samples, 3) if alternation_samples else 0.0
    pair_hit_rate = round(pair_hits / pair_samples, 3) if pair_samples else 0.0

    alternation_edge = alternation_match["score"] * max(alternation_hit_rate, 0.45)
    pair_edge = pair_match["score"] * max(pair_hit_rate, 0.45)
    if dragon_score >= 1.0 and dragon_score > alternation_match["score"] + 0.08 and dragon_score > pair_match["score"] + 0.08:
        rhythm_tag = "DRAGON_TREND"
    elif alternation_match["score"] >= 0.78 and alternation_edge > pair_edge + 0.06:
        rhythm_tag = "ALTERNATION_RHYTHM"
    elif pair_match["score"] >= 0.67 and pair_edge > alternation_edge + 0.04:
        rhythm_tag = "PAIR_FORMATION"
    else:
        rhythm_tag = "CHAOS_NOISE"

    chaos_score = round(max(0.0, 1.0 - max(alternation_match["score"], pair_match["score"], dragon_score)), 3)

    return {
        "recent_seq": recent_seq,
        "rhythm_tag": rhythm_tag,
        "alternation_score": alternation_match["score"],
        "alternation_pattern": alternation_match["pattern"],
        "alternation_next": alternation_match["next_char"],
        "alternation_hit_rate": alternation_hit_rate,
        "alternation_samples": alternation_samples,
        "pair_score": pair_match["score"],
        "pair_pattern": pair_match["pattern"],
        "pair_next": pair_match["next_char"],
        "pair_hit_rate": pair_hit_rate,
        "pair_samples": pair_samples,
        "dragon_score": dragon_score,
        "chaos_score": chaos_score,
        "pair_would_form_double": pair_would_form_double,
        "pair_would_chase_triple": pair_would_chase_triple,
    }


def _detect_alternation_break_signal(
    history: list,
    window: int = ALTERNATION_BREAK_TRIGGER_WINDOW,
    order: str = "near_to_far",
) -> Dict[str, Any]:
    """识别盘口“由近到远”纯交替信号，并给出结束交替的同向下注方向。"""
    if not isinstance(history, list) or len(history) < int(window):
        return {"active": False}

    normalized_order = str(order or "near_to_far").strip().lower()
    if normalized_order == "chronological":
        near_to_far = [int(x) for x in history[-int(window):][::-1]]
    else:
        near_to_far = [int(x) for x in history[:int(window)]]
    seq = "".join(str(x) for x in near_to_far)
    if seq not in ALTERNATION_BREAK_PATTERNS:
        return {"active": False}

    latest_value = int(near_to_far[0])
    return {
        "active": True,
        "near_to_far_seq": seq,
        "window": int(window),
        "latest_value": latest_value,
        "prediction": latest_value,
    }


def _clear_alternation_break_runtime(rt: dict) -> None:
    rt["alternation_break_active"] = False
    rt["alternation_break_seq"] = ""
    rt["alternation_break_side"] = ""


def _detect_fixed_pattern_signal(
    history: list,
    window: int = FIXED_PATTERN_TRIGGER_WINDOW,
) -> Dict[str, Any]:
    """识别固定数据序列信号，并给出相应的下注方向。"""
    if not isinstance(history, list) or len(history) < int(window):
        return {"active": False}

    near_to_far = [int(x) for x in history[-int(window):]]
    seq = "".join(str(x) for x in near_to_far)
    
    if seq not in FIXED_PATTERNS:
        return {"active": False}

    pattern_info = FIXED_PATTERNS[seq]
    follow_pattern = pattern_info["follow"]
    label = pattern_info["label"]
    
    latest_value = int(near_to_far[-1])  # 最新一手
    
    if follow_pattern == "reverse":
        # 反向下注：与最新一手相反
        prediction = 1 - latest_value
    elif len(follow_pattern) == 1:
        # 固定方向下注
        prediction = int(follow_pattern)
    else:
        # 默认按最新一手同向
        prediction = latest_value
    
    return {
        "active": True,
        "detected_seq": seq,
        "window": int(window),
        "follow_pattern": follow_pattern,
        "label": label,
        "prediction": prediction,
    }


def _apply_fixed_pattern_override(
    rt: dict,
    history: list,
    prediction: int,
) -> int:
    """在固定数据盘面里，按照检测到的规律下注（010101/101010 反向下注，111111/000000 延续下注）。"""
    signal = _detect_fixed_pattern_signal(history)
    if not signal.get("active", False):
        return int(prediction)

    forced_prediction = int(signal.get("prediction", prediction))
    side_text = "大" if forced_prediction == 1 else "小"
    detected_seq = str(signal.get("detected_seq", ""))
    label = str(signal.get("label", "固定规律"))
    follow_pattern = str(signal.get("follow_pattern", ""))
    near_to_far = [int(x) for x in history[-6:]]
    latest_value = int(near_to_far[-1])  # 最新一手
    
    # 构建原因说明
    if follow_pattern == "reverse":
        latest_side = "大" if latest_value == 1 else "小"
        predict_side = "小" if latest_side == "大" else "大"
        reason_text = f"检测到{label}（{detected_seq}），反向下注{predict_side}"
    else:
        reason_text = f"检测到{label}（{detected_seq}），按照规律下注{side_text}"
    
    rt["fixed_pattern_active"] = True
    rt["fixed_pattern_seq"] = detected_seq
    rt["fixed_pattern_side"] = side_text
    rt["fixed_pattern_label"] = label
    rt["last_predict_source"] = "fixed_pattern"
    rt["last_predict_tag"] = "FIXED_PATTERN"
    rt["last_predict_confidence"] = 100
    rt["last_predict_reason"] = reason_text
    rt["last_predict_info"] = _build_predict_basis_text(
        history=history,
        prediction=forced_prediction,
        source="fixed_pattern",
        pattern_tag="FIXED_PATTERN",
        rhythm_tag=label,
        tail_streak_len=len(follow_pattern) if len(follow_pattern) > 1 else 1,
        tail_streak_char=forced_prediction,
    )
    return forced_prediction


def _clear_fixed_pattern_runtime(rt: dict) -> None:
    rt["fixed_pattern_active"] = False
    rt["fixed_pattern_seq"] = ""
    rt["fixed_pattern_side"] = ""
    rt["fixed_pattern_label"] = ""


def _apply_alternation_break_override(
    rt: dict,
    history: list,
    prediction: int,
    *,
    order: str = "near_to_far",
) -> int:
    """在纯交替盘面里，用“最新一手同向”增强主脚本，而不是替换整套模型。"""
    signal = _detect_alternation_break_signal(history, order=order)
    if not signal.get("active", False):
        _clear_alternation_break_runtime(rt)
        return int(prediction)

    forced_prediction = int(signal.get("prediction", prediction))
    side_text = "大" if forced_prediction == 1 else "小"
    window = int(signal.get("window", ALTERNATION_BREAK_TRIGGER_WINDOW))
    rt["alternation_break_active"] = True
    rt["alternation_break_seq"] = str(signal.get("near_to_far_seq", ""))
    rt["alternation_break_side"] = side_text
    rt["last_predict_source"] = "alternation_break"
    rt["last_predict_tag"] = "ALTERNATION_BREAK"
    rt["last_predict_confidence"] = 100
    rt["last_predict_reason"] = f"{window}位纯交替，按结束交替规则押同向"
    rt["last_predict_info"] = _build_predict_basis_text(
        history=history,
        prediction=forced_prediction,
        source="alternation_break",
        pattern_tag="ALTERNATION_BREAK",
        rhythm_tag="ALTERNATION_RHYTHM",
        tail_streak_len=window,
        tail_streak_char=forced_prediction,
    )
    return forced_prediction


def fallback_prediction(history):
    """
    统计兜底预测。
    当模型不可用时，优先补最近窗口里相对偏少的一侧。
    """
    if not history:
        return 1
    
    window = min(40, len(history))
    recent = history[-window:]
    big_count = sum(recent)
    small_count = window - big_count
    
    prediction = 1 if big_count < small_count else 0
    
    log_event(logging.WARNING, 'predict_core', '统计兜底触发', 
              user_id=0, data=f'big={big_count}, small={small_count}, fallback={prediction}')
    
    return prediction


_PREDICT_PATTERN_LABELS = {
    "ALTERNATION_RHYTHM": "交替偏强",
    "PAIR_FORMATION": "配对偏强",
    "DRAGON_TREND": "长龙偏强",
    "LONG_DRAGON": "长龙明显",
    "DRAGON_CANDIDATE": "连势抬头",
    "DOUBLE_STREAK": "二连信号",
    "SINGLE_JUMP": "交替偏强",
    "SYMMETRIC_WRAP": "盘面偏乱",
    "CHAOS_SWITCH": "盘面偏乱",
    "CHAOS_NOISE": "盘面偏乱",
    "ALTERNATION_BREAK": "6位纯交替",
    "TIMEOUT_FALLBACK": "信号不足",
    "INVALID_FALLBACK": "信号不足",
    "FALLBACK": "信号不足",
    "UNLOCK": "信号不足",
}


def _format_predict_sequence_block(history: list, window: int = 5) -> str:
    if not isinstance(history, list) or not history:
        return "[]"
    actual = min(max(3, int(window)), len(history))
    seq = [str(int(x)) for x in history[-actual:]]
    return "[" + " ".join(seq) + "]"


def _format_predict_window_text_from_counts(big_count: int, small_count: int) -> str:
    try:
        big_count = int(big_count)
    except Exception:
        big_count = 0
    try:
        small_count = int(small_count)
    except Exception:
        small_count = 0
    diff = abs(big_count - small_count)
    if diff <= 1:
        return "大小基本均衡"
    if small_count > big_count:
        return f"小比大多 {diff} 次"
    return f"大比小多 {diff} 次"


def _format_predict_near_window_text(history: list, window: int = 40) -> str:
    if not isinstance(history, list) or not history:
        return "数量样本不够，先当背景参考"
    actual = min(max(1, int(window)), len(history))
    recent = [int(x) for x in history[-actual:]]
    big_count = sum(recent)
    small_count = actual - big_count
    diff = abs(big_count - small_count)
    if diff <= 1:
        return "数量接近，先当背景看"
    if big_count > small_count:
        return f"数量偏大（大比小多 {diff} 次）"
    return f"数量偏小（小比大多 {diff} 次）"


def _format_predict_far_window_text(history: list, window: int = 100) -> str:
    if not isinstance(history, list) or not history:
        return "长线样本不够，先不看太重"
    actual = min(max(1, int(window)), len(history))
    recent = [int(x) for x in history[-actual:]]
    big_count = sum(recent)
    small_count = actual - big_count
    diff = big_count - small_count
    if diff > 5:
        return "长期偏大，大数略占上风"
    if diff < -5:
        return "长期偏小，小数略占上风"
    return "长期分布接近均衡"


def _format_predict_short_window_text(
    history: list,
    *,
    pattern_tag: str = "",
    rhythm_tag: str = "",
) -> str:
    if not isinstance(history, list) or not history:
        return "短线样本不足，先别放大看"

    rhythm_key = str(rhythm_tag or "").strip().upper()
    pattern_key = str(pattern_tag or "").strip().upper()
    if rhythm_key == "DRAGON_TREND" or pattern_key in {"LONG_DRAGON", "DRAGON_CANDIDATE"}:
        return "短线顺着走，反转不明显"
    if rhythm_key == "ALTERNATION_RHYTHM" or pattern_key in {"SINGLE_JUMP", "ALTERNATION_BREAK"}:
        return "短线来回切换，交替还在延续"
    if rhythm_key == "PAIR_FORMATION" or pattern_key == "DOUBLE_STREAK":
        return "短线更像成对，节奏在配"

    actual = min(20, len(history))
    recent = [int(x) for x in history[-actual:]]
    big_count = sum(recent)
    small_count = actual - big_count
    if abs(big_count - small_count) <= 2:
        return "短线还在拉扯，方向不够稳"
    return "短线有点偏一边，但还不算很稳"


def _format_predict_tail_shape_text(
    *,
    pattern_tag: str = "",
    tail_streak_len: int = 0,
    tail_streak_char: Any = None,
    rhythm_tag: str = "",
    history: Optional[list] = None,
) -> str:
    side_text = "大" if str(tail_streak_char) == "1" or tail_streak_char == 1 else "小"
    seq_block = _format_predict_sequence_block(history or [], 5)
    pattern_key = str(pattern_tag or "").strip().upper()
    rhythm_key = str(rhythm_tag or "").strip().upper()

    if pattern_key in {"LONG_DRAGON", "DRAGON_CANDIDATE"} and int(tail_streak_len or 0) > 0:
        return f"{int(tail_streak_len)}连{side_text} {seq_block}，还没断"
    if rhythm_key == "ALTERNATION_RHYTHM" or pattern_key in {"SINGLE_JUMP", "ALTERNATION_BREAK"}:
        actual = min(max(3, 5), len(history or []))
        return f"{actual}位单跳 {seq_block}，还在来回跳"
    if rhythm_key == "PAIR_FORMATION" or pattern_key == "DOUBLE_STREAK":
        return f"{seq_block}，更像配对结构"
    return f"{seq_block}，形态还没走顺"


def _resolve_predict_model_judgment_text(
    *,
    source: str,
    pattern_tag: str = "",
    rhythm_tag: str = "",
    raw_reason: str = "",
    prediction: int,
) -> str:
    source_text = str(source or "").strip().lower()
    if source_text in MODEL_WAIT_SOURCES:
        return "当前模型链不可用，等待恢复"
    if source_text == "alternation_break":
        return "交替拉满了，直接按打断规则走"
    if source_text == "unlock_fallback":
        return "连续观望太久，系统先保守出手"
    if source_text in {"timeout_fallback", "invalid_fallback", "hard_fallback", "fallback", "fallback_skip"}:
        return "模型这局没给稳定答案，先走系统兜底"

    rhythm_key = str(rhythm_tag or "").strip().upper()
    pattern_key = str(pattern_tag or "").strip().upper()
    if rhythm_key == "DRAGON_TREND" or pattern_key in {"LONG_DRAGON", "DRAGON_CANDIDATE"}:
        return "长龙更顺，继续顺着走更省力"
    if rhythm_key == "PAIR_FORMATION" or pattern_key == "DOUBLE_STREAK":
        return "更像要补成双边，先往配对方向看"
    if rhythm_key == "ALTERNATION_RHYTHM" or pattern_key == "SINGLE_JUMP":
        return "交替结构更强，先顺着节奏看"
    if int(prediction) == -1:
        return "信号打架了，先别硬上"

    normalized = str(raw_reason or "").strip()
    if normalized:
        return normalized
    return "当前一边稍强，先跟着优势走"


def _resolve_predict_pattern_text(
    *,
    source: str,
    pattern_tag: str,
    rhythm_tag: str,
) -> str:
    source_text = str(source or "").strip().lower()
    if source_text in MODEL_WAIT_SOURCES:
        return "模型不可用"
    if source_text == "alternation_break":
        return "6位纯交替"
    if source_text in {"timeout_fallback", "invalid_fallback", "hard_fallback", "fallback", "fallback_skip", "unlock_fallback"}:
        return "信号不足"

    rhythm_key = str(rhythm_tag or "").strip().upper()
    if rhythm_key in _PREDICT_PATTERN_LABELS and rhythm_key not in {"CHAOS_NOISE"}:
        return _PREDICT_PATTERN_LABELS[rhythm_key]

    pattern_key = str(pattern_tag or "").strip().upper()
    return _PREDICT_PATTERN_LABELS.get(pattern_key, "盘面偏乱")


def _resolve_predict_conclusion_text(*, source: str, prediction: int) -> str:
    source_text = str(source or "").strip().lower()
    if source_text in MODEL_WAIT_SOURCES:
        return "等待模型恢复后再下注"
    if int(prediction) == -1:
        return "这局先【观望】"
    if int(prediction) == 1:
        return "本局坚决押【大】"
    return "本局坚决押【小】"


def _build_predict_basis_text(
    *,
    history: list,
    prediction: int,
    source: str,
    pattern_tag: str = "",
    rhythm_tag: str = "",
    near_text: str = "",
    far_text: str = "",
    pattern_text: str = "",
    short_text: str = "",
    tail_text: str = "",
    model_text: str = "",
    raw_reason: str = "",
    tail_streak_len: int = 0,
    tail_streak_char: Any = None,
) -> str:
    pattern_display = str(pattern_text or "").strip() or _resolve_predict_pattern_text(
        source=source,
        pattern_tag=pattern_tag,
        rhythm_tag=rhythm_tag,
    )
    short_display = str(short_text or "").strip() or _format_predict_short_window_text(
        history,
        pattern_tag=pattern_tag,
        rhythm_tag=rhythm_tag,
    )
    near_display = str(near_text or "").strip() or _format_predict_near_window_text(history, 40)
    far_display = str(far_text or "").strip() or _format_predict_far_window_text(history, 100)
    tail_display = str(tail_text or "").strip() or _format_predict_tail_shape_text(
        pattern_tag=pattern_tag,
        tail_streak_len=tail_streak_len,
        tail_streak_char=tail_streak_char,
        rhythm_tag=rhythm_tag,
        history=history,
    )
    model_display = str(model_text or "").strip() or _resolve_predict_model_judgment_text(
        source=source,
        pattern_tag=pattern_tag,
        rhythm_tag=rhythm_tag,
        raw_reason=raw_reason,
        prediction=prediction,
    )
    conclusion_text = _resolve_predict_conclusion_text(source=source, prediction=prediction)
    return (
        "🤖 决策依据\n"
        f"├ 📊 100局： {far_display}\n"
        f"├ 🌊 40局： {near_display}\n"
        f"├ ⚡ 20局： {short_display}\n"
        f"├ 🧬 5局： {tail_display}\n"
        f"├ 🤖 大模型： {model_display}\n"
        f"└ 🎯 押注结论： {conclusion_text}"
    )


_PATTERN_LABELS = {
    "LONG_DRAGON": "盘面连势明显",
    "DRAGON_CANDIDATE": "连势开始抬头",
    "DOUBLE_STREAK": "刚出现二连信号",
    "SINGLE_JUMP": "单双交替明显",
    "SYMMETRIC_WRAP": "盘面有来回拉扯",
    "CHAOS_SWITCH": "盘面切换频繁",
    "CHAOS_NOISE": "节奏偏乱",
}

_RHYTHM_LABELS = {
    "ALTERNATION_RHYTHM": "单双交替更明显",
    "PAIR_FORMATION": "成对走势更明显",
    "DRAGON_TREND": "顺势延续更明显",
    "CHAOS_NOISE": "节奏不稳定",
}

_REASON_REPLACEMENTS = [
    ("alternation rhythm dominates", "单双交替更明显"),
    ("pair formation rhythm dominates", "成对走势更明显"),
    ("dragon trend dominates", "顺势延续更明显"),
    ("chaos rhythm", "盘面偏乱"),
    ("chaos switch", "盘面切换频繁"),
    ("weak evidence", "依据偏弱"),
    ("weak pair formation signal", "成对信号偏弱"),
    ("supporting double streak evidence", "二连信号有一定支持"),
    ("supporting double streak", "二连信号有一定支持"),
    ("double streak", "二连信号"),
    ("history hit rate supports alternation continuation", "历史命中率支持继续交替"),
    ("history hit rate supports it", "历史命中率支持这个判断"),
    ("despite chaos entropy tag", "虽然盘面有些乱"),
    ("no streak support", "没有明显连势支撑"),
    ("pair formation signal", "成对信号"),
    ("pair formation", "成对走势"),
    ("alternation continuation", "继续走交替"),
    ("alternation", "交替走势"),
    ("skip", "先观望"),
]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _normalize_reason_text(raw_reason: str) -> str:
    text = str(raw_reason or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for old, new in _REASON_REPLACEMENTS:
        lowered = lowered.replace(old, new)
    lowered = lowered.replace("_", " ").replace("/", " ").replace("|", "，")
    lowered = re.sub(r"\s+", " ", lowered).strip(" ,，。")
    lowered = lowered.replace("  ", " ")
    lowered = lowered.replace(" ,", "，").replace(",", "，")
    lowered = lowered.replace(" .", "。").replace(".", "。")
    lowered = lowered.replace(" ;", "；").replace(";", "；")
    lowered = re.sub(r"\b(with|and|but|despite|while|because|the|a|an|to|of|for)\b", " ", lowered, flags=re.I)
    lowered = re.sub(r"\s+", " ", lowered).strip(" ，。；")
    return lowered


def _fallback_reason_text(
    pattern_tag: str,
    rhythm_tag: str,
    prediction: int,
    confidence: int,
) -> str:
    pieces: List[str] = []
    pattern_text = _PATTERN_LABELS.get(str(pattern_tag or "").upper(), "")
    rhythm_text = _RHYTHM_LABELS.get(str(rhythm_tag or "").upper(), "")
    if pattern_text:
        pieces.append(pattern_text)
    if rhythm_text and rhythm_text != pattern_text:
        pieces.append(rhythm_text)

    if prediction == -1:
        if confidence < 40:
            pieces.append("把握偏低，先观望一局")
        else:
            pieces.append("先观察一局更稳")
    else:
        if confidence < 40:
            pieces.append("把握偏低")
        elif confidence < 70:
            pieces.append("把握一般")
        else:
            pieces.append("把握较强")

    text = "，".join(piece for piece in pieces if piece)
    return text or ("当前信号不清晰，先观望一局" if prediction == -1 else "当前信号可参考")


def _humanize_predict_reason(
    raw_reason: str,
    pattern_tag: str,
    rhythm_tag: str,
    prediction: int,
    confidence: int,
) -> str:
    normalized = _normalize_reason_text(raw_reason)
    if not normalized or not _has_cjk(normalized):
        normalized = _fallback_reason_text(pattern_tag, rhythm_tag, prediction, confidence)
    else:
        normalized = re.sub(r"\s+", " ", normalized).strip(" ，。；")
        if prediction == -1 and not any(word in normalized for word in ("观望", "跳过", "先看", "先等")):
            normalized = f"{normalized}，先观望一局"

    parts = [part.strip(" ，。；") for part in re.split(r"[，。；]+", normalized) if part.strip(" ，。；")]
    compact = "，".join(parts[:2]) if parts else normalized
    return compact[:36].rstrip("，")


def parse_analysis_result_insight(resp_text, default_prediction=1):
    """
    解析 AI 输出，返回 prediction/confidence/reason。
    prediction 允许: 1(大) / 0(小) / -1(SKIP)
    """
    try:
        cleaned = str(resp_text).replace('```json', '').replace('```', '').strip()
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
        resp_json = json.loads(cleaned)
        
        prediction = resp_json.get('prediction', default_prediction)
        if isinstance(prediction, str):
            pred_norm = prediction.strip().upper()
            if pred_norm in {'-1', 'SKIP', 'NONE', 'PASS', 'WAIT', '观望', '跳过'}:
                prediction = -1
            elif pred_norm in {'1', 'B', 'BIG', '大'}:
                prediction = 1
            elif pred_norm in {'0', 'S', 'SMALL', '小'}:
                prediction = 0
            else:
                prediction = default_prediction
        try:
            prediction = int(prediction)
        except Exception:
            prediction = default_prediction
        if prediction not in [-1, 0, 1]:
            prediction = default_prediction
        
        confidence = int(resp_json.get('confidence', 50))
        confidence = max(0, min(100, confidence))
        
        reason = resp_json.get('reason', resp_json.get('logic', '模型分析'))
        
        return {
            'prediction': prediction,
            'confidence': confidence,
            'reason': reason
        }
    except Exception as e:
        return {
            'prediction': default_prediction,
            'confidence': 50,
            'reason': f'解析兜底:{str(e)[:20]}'
        }


# 主预测函数
async def predict_next_bet_core(user_ctx: UserContext, global_config: dict, current_round: int = 1) -> int:
    """
    根据历史节奏、统计特征和模型输出来决定本局方向。
    """
    state = user_ctx.state
    rt = state.runtime
    history = state.history
    
    try:
        stat_fallback_enabled = _is_stat_fallback_bet_enabled(user_ctx)
        rt["stat_fallback_bet_enabled"] = stat_fallback_enabled

        # 第一步：构建历史窗口快照。
        
        # 短期窗口（20局）
        short_term_20 = history[-20:] if len(history) >= 20 else history[:]
        short_str = "".join(['1' if x == 1 else '0' for x in short_term_20])

        # 近端平衡窗口（40局）
        near_40_gap = calculate_trend_gap(history, window=40)
        near_40_big = int(near_40_gap.get("big_count", 0) or 0)
        near_40_small = int(near_40_gap.get("small_count", 0) or 0)
        near_40_text = _format_predict_window_text_from_counts(near_40_big, near_40_small)
        
        # 中期窗口（50局）
        medium_term_50 = history[-50:] if len(history) >= 50 else history[:]
        medium_str = "".join(['1' if x == 1 else '0' for x in medium_term_50])
        
        # 长期窗口（100局）
        long_term_100 = history[-100:] if len(history) >= 100 else history[:]
        long_term_gap = round(sum(long_term_100) / len(long_term_100), 3) if long_term_100 else 0.5
        
        # 趋势缺口
        trend_gap = calculate_trend_gap(history, window=100)
        big_cnt = trend_gap['big_count']
        small_cnt = trend_gap['small_count']
        gap = trend_gap['gap']
        
        # 形态与节奏特征
        pattern_features = extract_pattern_features(history)
        pattern_tag = pattern_features['pattern_tag']
        tail_streak_len = pattern_features['tail_streak_len']
        tail_streak_char = pattern_features['tail_streak_char']
        double_streak_stats = analyze_double_streak_followups(history)
        rhythm_context = analyze_rhythm_context(history)
        
        # 当前连押压力标签
        lose_count = rt.get('lose_count', 0)
        entropy_tag = "Pattern_Breaking" if lose_count > 2 else "Stability"
        
        # 第二步：整理模型输入上下文。
        
        payload = {
            "current_status": {
                "martingale_step": lose_count + 1,
                "total_profit_to_date": rt.get('earnings', 0),
                "entropy_tag": entropy_tag
            },
            "history_views": {
                "short_term_20": short_str,
                "near_term_40_text": near_40_text,
                "near_term_40_big_count": near_40_big,
                "near_term_40_small_count": near_40_small,
                "medium_term_50": medium_str,
                "long_term_gap": long_term_gap,
                "big_count_100": big_cnt,
                "small_count_100": small_cnt
            },
            "pattern_analysis": {
                "tag": pattern_tag,
                "tail_streak_len": tail_streak_len,
                "tail_streak_char": tail_streak_char,
                "gap": f"{gap:+d}"
            },
            "double_streak_analysis": {
                "current_side": double_streak_stats["current_side"],
                "sample_count": double_streak_stats["current_side_total"],
                "continue_count": double_streak_stats["current_continue"],
                "reverse_count": double_streak_stats["current_reverse"],
                "continue_rate": double_streak_stats["current_continue_rate"],
                "reverse_rate": double_streak_stats["current_reverse_rate"],
                "preference": double_streak_stats["current_preference"],
            },
            "rhythm_analysis": {
                "tag": rhythm_context["rhythm_tag"],
                "recent_seq": rhythm_context["recent_seq"],
                "alternation_score": rhythm_context["alternation_score"],
                "alternation_pattern": rhythm_context["alternation_pattern"],
                "alternation_next": rhythm_context["alternation_next"],
                "alternation_hit_rate": rhythm_context["alternation_hit_rate"],
                "alternation_samples": rhythm_context["alternation_samples"],
                "pair_score": rhythm_context["pair_score"],
                "pair_pattern": rhythm_context["pair_pattern"],
                "pair_next": rhythm_context["pair_next"],
                "pair_hit_rate": rhythm_context["pair_hit_rate"],
                "pair_samples": rhythm_context["pair_samples"],
                "dragon_score": rhythm_context["dragon_score"],
                "chaos_score": rhythm_context["chaos_score"],
                "pair_would_form_double": rhythm_context["pair_would_form_double"],
                "pair_would_chase_triple": rhythm_context["pair_would_chase_triple"],
            }
        }
        
        # 第三步：构建推理提示词。
        
        current_model_id = rt.get('current_model_id', 'qwen3-coder-plus')
        actual_model_id = current_model_id
        prompt = f"""[System Instruction]
You are a quantitative trading analyst for a binary big/small game. First identify the dominant rhythm of the board, then decide whether it deserves a bet. If evidence is weak or conflicting, output SKIP (-1).

[Pattern Priority]
1. LONG_DRAGON: tail streak >= 4. This is now a mature dragon pattern.
2. DRAGON_CANDIDATE: tail streak == 3.
3. DOUBLE_STREAK: tail streak == 2. This is useful, but it is not enough by itself.
4. Rhythm layer: alternation rhythm vs pair formation rhythm.
5. SINGLE_JUMP / SYMMETRIC_WRAP / CHAOS_SWITCH are weaker transition structures.

[Rhythm Layer]
- rhythm_tag: {rhythm_context['rhythm_tag']}
- recent_seq: {rhythm_context['recent_seq']}
- alternation_score: {rhythm_context['alternation_score']:.3f}
- alternation_pattern: {rhythm_context['alternation_pattern']}
- alternation_expected_next: {rhythm_context['alternation_next']}
- alternation_hit_rate: {rhythm_context['alternation_hit_rate']:.3f} (samples={rhythm_context['alternation_samples']})
- pair_score: {rhythm_context['pair_score']:.3f}
- pair_pattern: {rhythm_context['pair_pattern']}
- pair_expected_next: {rhythm_context['pair_next']}
- pair_hit_rate: {rhythm_context['pair_hit_rate']:.3f} (samples={rhythm_context['pair_samples']})
- pair_would_form_double: {str(rhythm_context['pair_would_form_double']).lower()}
- pair_would_chase_triple: {str(rhythm_context['pair_would_chase_triple']).lower()}
- dragon_score: {rhythm_context['dragon_score']:.3f}
- chaos_score: {rhythm_context['chaos_score']:.3f}

[Rhythm Rules]
1. If alternation_score is clearly stronger than pair_score and the history hit rate also supports it, treat the board as ALTERNATION_RHYTHM. Follow alternation_expected_next instead of guessing that alternation will suddenly break.
2. If pair_score is clearly stronger than alternation_score and the history hit rate supports it, treat the board as PAIR_FORMATION. Favor pair_expected_next only when it is trying to form the next double.
3. If pair_would_chase_triple is true, reduce confidence sharply. Pair logic is mainly for forming the next 2-streak, not for aggressively chasing 3-streak.
4. If recent_seq is a long pure alternation chain and no real double has appeared yet, be very cautious about betting against alternation. Pair bets need clearly better evidence.
5. If alternation_score and pair_score are close, or rhythm_tag is CHAOS_NOISE, lower confidence first. Only output SKIP when neither side has a usable edge.

[Double Streak Rule]
- side: {double_streak_stats['current_side']}
- sample_count: {double_streak_stats['current_side_total']}
- continue_count: {double_streak_stats['current_continue']}
- reverse_count: {double_streak_stats['current_reverse']}
- continue_rate: {double_streak_stats['current_continue_rate']:.3f}
- reverse_rate: {double_streak_stats['current_reverse_rate']:.3f}
- preference: {double_streak_stats['current_preference']}
Interpretation:
- DOUBLE_STREAK is a supporting clue, not the only clue.
- If pair rhythm is strong and the next hand would form a fresh double, DOUBLE_STREAK can be weighted higher.
- If DOUBLE_STREAK already exists and the next hand would directly chase a triple, lower its weight.

[Hard Risk Rules]
1. If martingale_step >= {HIGH_PRESSURE_SKIP_MIN_STEP} and confidence < {HIGH_PRESSURE_SKIP_MIN_CONF}, do not rush to SKIP. First check whether one side still has clearer rhythm support; if yes, you may still bet with reduced confidence.
2. If pattern tag is CHAOS_SWITCH / SINGLE_JUMP / SYMMETRIC_WRAP and martingale_step >= 3, be conservative but do not default to SKIP. Only SKIP when the board is both unstable and direction evidence is clearly conflicting.
3. DRAGON_CANDIDATE is not enough by itself in high-pressure hands, but it can still support a bet when rhythm evidence points in the same direction.
4. If long_term_gap is near neutral [{NEUTRAL_LONG_TERM_GAP_LOW:.2f}, {NEUTRAL_LONG_TERM_GAP_HIGH:.2f}], treat long-term distribution as weak evidence, not zero evidence.
5. If trend evidence and reversal evidence conflict sharply, output SKIP. If one side still has a slight but usable edge, keep prediction 0 or 1 and lower confidence.

[Data Evidence]
short_term_20: {short_str}
near_term_40: big={near_40_big}, small={near_40_small}, summary={near_40_text}
medium_term_50: {medium_str}
long_term_big_ratio: {long_term_gap:.2f}
pattern_tag: {pattern_tag}
tail_streak_len: {tail_streak_len}
tail_side: {'big' if tail_streak_char == 1 else 'small'}
gap: {gap:+d}
martingale_step: {lose_count + 1}
entropy_tag: {entropy_tag}

[Output Policy]
- Decide the dominant board rhythm first: dragon / alternation / pair / chaos.
- If alternation rhythm dominates, prefer the alternation continuation side.
- If pair rhythm dominates, prefer the side that forms the next double.
- Prefer giving prediction 0 or 1 whenever one side still has a usable edge.
- Only output SKIP when the board is truly unreadable, evidence is sharply conflicting, or both directions lack usable support.

[Language Rules]
- reasoning 必须使用简体中文，面向普通用户，避免英文术语和标签名。
- reasoning 尽量短，控制在 12 到 28 个汉字左右。
- 可以像这样表达：`盘面偏乱，配对信号弱，先观望一局`。

[Response Format]
Return JSON only:
{{"logic": "short summary", "reasoning": "why bet or skip", "confidence": 1-100, "prediction": -1 or 0 or 1}}"""

        messages = [
            {'role': 'system', 'content': '你是专门破解博弈陷阱的量化交易员，只输出纯JSON。prediction 仅允许 -1/0/1。'},
            {'role': 'user', 'content': prompt}
        ]
        
        log_event(logging.INFO, 'predict_core', f'模型分析调用: {current_model_id}', 
                  user_id=user_ctx.user_id, data=f'形态:{pattern_tag} 缺口:{gap:+d} 压力:{lose_count + 1}次')
        
        # 第四步：调用模型并处理降级。

        model_used = True
        try:
            configured_keys = _normalize_ai_keys(user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {})
            if not configured_keys:
                raise Exception("AI_KEY_MISSING")

            result = await user_ctx.get_model_manager().call_model(
                current_model_id,
                messages,
                temperature=0.1,
                max_tokens=500
            )
            if not result['success']:
                raise Exception(f"Model Error: {result['error']}")

            _clear_ai_key_issue(rt)
            rt["last_model_notice_sig_failure"] = ""
            actual_model_id = str(result.get("model_id") or current_model_id)
            if actual_model_id != current_model_id:
                switch_detail = "旧模型已不可用，已切到当前备用模型。"
                if str(result.get("requested_model_id") or "") != actual_model_id:
                    switch_detail = "原模型不可用，系统已按当前降级链自动切换。"
                _queue_model_notice(
                    rt,
                    "switch",
                    signature=f"{current_model_id}->{actual_model_id}",
                    from_model=current_model_id,
                    to_model=actual_model_id,
                    detail=switch_detail,
                )
                rt["current_model_id"] = actual_model_id
                _mark_model_success(rt, actual_model_id, switched_from=current_model_id)
                log_event(
                    logging.WARNING,
                    'predict_core',
                    '主模型不可用，已按排序自动降级',
                    user_id=user_ctx.user_id,
                    data=f'{current_model_id} -> {actual_model_id}'
                )
                user_ctx.save_state()
                current_model_id = actual_model_id
            else:
                _mark_model_success(rt, actual_model_id)
            
            default_pred = trend_gap['regression_target']
            final_result = parse_analysis_result_insight(result['content'], default_prediction=default_pred)
            
        except Exception as model_error:
            model_used = False
            err_text = str(model_error)
            if "AI_KEY_MISSING" in err_text:
                _mark_ai_key_issue(rt, "未配置可用 api_keys")
            elif _looks_like_ai_key_issue(err_text):
                _mark_ai_key_issue(rt, err_text)
            _queue_model_notice(
                rt,
                "failure",
                signature=f"{current_model_id}|{_summarize_model_error(err_text)}",
                from_model=current_model_id,
                detail=_summarize_model_error(err_text),
            )
            _mark_model_failure(rt, "fallback" if stat_fallback_enabled else "model_wait", err_text)
            log_event(logging.WARNING, 'predict_core', '模型调用失败', 
                      user_id=user_ctx.user_id, data=err_text)
            if stat_fallback_enabled:
                final_result = {
                    'prediction': trend_gap['regression_target'],
                    'confidence': 50,
                    'reason': '模型异常，统计回归兜底'
                }
            else:
                final_result = {
                    'prediction': -1,
                    'confidence': 0,
                    'reason': '模型链不可用，等待恢复',
                    'wait_for_model': True,
                }
        
        # 第五步：校验输出并写回运行态。
        
        prediction = final_result['prediction']
        confidence = final_result['confidence']
        reason = final_result.get('reason', final_result.get('logic', '深度分析'))
        
        if prediction not in [-1, 0, 1]:
            prediction = trend_gap['regression_target']
            confidence = 50
            reason = '强制校正：统计回归'
        
        user_reason = _humanize_predict_reason(
            reason,
            pattern_tag,
            rhythm_context['rhythm_tag'],
            int(prediction),
            int(confidence),
        )

        # 构建预测信息
        rt["last_predict_tag"] = pattern_tag
        rt["last_predict_confidence"] = int(confidence)
        if final_result.get("wait_for_model", False):
            rt["last_predict_source"] = "model_wait"
        elif prediction == -1:
            rt["last_predict_source"] = "model_skip" if model_used else "fallback_skip"
        else:
            rt["last_predict_source"] = "model" if model_used else "fallback"
        rt["last_predict_reason"] = user_reason
        rt["last_predict_gap"] = int(gap)
        rt["last_predict_long_term_gap"] = float(long_term_gap)
        rt["last_predict_tail_len"] = int(tail_streak_len)
        rt["last_predict_tail_char"] = int(tail_streak_char)
        rt["last_predict_info"] = _build_predict_basis_text(
            history=history,
            prediction=int(prediction),
            source=str(rt.get("last_predict_source", "") or ""),
            pattern_tag=pattern_tag,
            rhythm_tag=str(rhythm_context.get("rhythm_tag", "") or ""),
            near_text=near_40_text,
            far_text=_format_predict_far_window_text(long_term_100, 100),
            short_text=_format_predict_short_window_text(
                history,
                pattern_tag=pattern_tag,
                rhythm_tag=str(rhythm_context.get("rhythm_tag", "") or ""),
            ),
            tail_text=_format_predict_tail_shape_text(
                pattern_tag=pattern_tag,
                rhythm_tag=str(rhythm_context.get("rhythm_tag", "") or ""),
                tail_streak_len=tail_streak_len,
                tail_streak_char=tail_streak_char,
                history=history,
            ),
            raw_reason=user_reason,
            tail_streak_len=tail_streak_len,
            tail_streak_char=tail_streak_char,
        )
        
        # 审计日志
        audit_log = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "round": current_round,
            "mode": "core_predictor",
            "input_payload": payload,
            "output": final_result,
            "model_id": actual_model_id,
            "prediction_source": rt.get("last_predict_source", "unknown"),
            "pattern_tag": pattern_tag,
        }
        rt["last_logic_audit"] = json.dumps(audit_log, ensure_ascii=False, indent=2)
        
        # 记录预测
        state.predictions.append(prediction)
        
        if _verbose_runtime_diag_enabled():
            log_event(logging.INFO, 'predict_core', '模型分析完成', 
                      user_id=user_ctx.user_id, data=f'pred={prediction}, conf={confidence}, pattern={pattern_tag}')
        
        return prediction
        
    except Exception as e:
        log_event(logging.ERROR, 'predict_core', '核心预测异常，使用最终保底', 
                  user_id=user_ctx.user_id, data=str(e))
        
        recent_20 = history[-20:] if len(history) >= 20 else history
        recent_sum = sum(recent_20)
        fallback = 0 if recent_sum >= len(recent_20) / 2 else 1
        
        rt["last_predict_tag"] = "FALLBACK"
        rt["last_predict_confidence"] = 0
        rt["last_predict_source"] = "hard_fallback"
        rt["last_predict_reason"] = "模型异常最终兜底"
        rt["last_predict_info"] = _build_predict_basis_text(
            history=history,
            prediction=int(fallback),
            source="hard_fallback",
            pattern_tag="FALLBACK",
            rhythm_tag="CHAOS_NOISE",
            tail_streak_len=int(pattern_features.get("tail_streak_len", 0) or 0) if 'pattern_features' in locals() else 0,
            tail_streak_char=pattern_features.get("tail_streak_char", None) if 'pattern_features' in locals() else None,
        )
        state.predictions.append(fallback)
        return fallback


# 押注处理
async def _refresh_dashboard_message_slim(client, user_ctx: UserContext, global_config: dict):
    dashboard = format_dashboard(user_ctx)
    if hasattr(user_ctx, "dashboard_message") and user_ctx.dashboard_message:
        await cleanup_message(client, user_ctx.dashboard_message)
    user_ctx.dashboard_message = await send_message_v2(
        client,
        "dashboard",
        dashboard,
        user_ctx,
        global_config,
        parse_mode="html",
    )
    return user_ctx.dashboard_message


async def _push_market_broadcast_snapshot(user_ctx: UserContext, history: List[int]) -> None:
    if not history:
        return
    try:
        from market_broadcast_alert.market_broadcast_alert import process_market_history_snapshot

        await asyncio.to_thread(process_market_history_snapshot, list(history))
    except Exception as e:
        log_event(
            logging.WARNING,
            'market_alert',
            '盘口播报快照处理失败',
            user_id=user_ctx.user_id,
            data=str(e),
        )


async def _process_bet_on_slim(client, event, user_ctx: UserContext, global_config: dict):
    state = user_ctx.state
    rt = state.runtime

    timing_cfg = _read_timing_config(global_config)
    prompt_wait_sec = timing_cfg["prompt_wait_sec"]
    predict_timeout_sec = timing_cfg["predict_timeout_sec"]
    click_interval_sec = timing_cfg["click_interval_sec"]
    click_timeout_sec = timing_cfg["click_timeout_sec"]
    flow_started_at = time.monotonic()
    prompt_wait_ms = 0
    predict_ms = 0
    click_ms = 0

    def _emit_bet_timing(outcome: str, **extra: Any) -> None:
        total_ms = int((time.monotonic() - flow_started_at) * 1000)
        timing_payload = {
            "timing_outcome": str(outcome or "").strip() or "unknown",
            "timing_total_ms": total_ms,
            "timing_prompt_wait_ms": int(prompt_wait_ms),
            "timing_predict_ms": int(predict_ms),
            "timing_click_ms": int(click_ms),
            "timing_predict_budget_ms": int(predict_timeout_sec * 1000),
            "timing_click_budget_ms": int(click_timeout_sec * 1000),
        }
        if extra:
            timing_payload.update(extra)
        log_event(
            logging.INFO,
            'bet_on',
            '下注时序摘要',
            user_id=user_ctx.user_id,
            category='runtime',
            **_build_runtime_chain_diag(rt, state, **timing_payload),
        )

    if not getattr(event, "reply_markup", None) and prompt_wait_sec > 0:
        prompt_wait_started_at = time.monotonic()
        await asyncio.sleep(prompt_wait_sec)
        prompt_wait_ms = int((time.monotonic() - prompt_wait_started_at) * 1000)

    text = event.message.message
    history_before = list(state.history)
    incoming_history: List[int] = []
    history_changed = False
    try:
        incoming_history = _extract_history_from_bet_on_text(text)
        if incoming_history and len(incoming_history) >= len(history_before):
            state.history = incoming_history[-2000:]
            history_changed = state.history != history_before
    except Exception as e:
        log_event(logging.WARNING, 'bet_on', '解析历史数据失败', user_id=user_ctx.user_id, data=str(e))

    if history_changed:
        await _push_market_broadcast_snapshot(user_ctx, state.history)

    next_bet_amount_snapshot = calculate_bet_amount(rt)
    if _verbose_runtime_diag_enabled():
        log_event(
            logging.INFO,
            'bet_on',
            '下注入口诊断',
            user_id=user_ctx.user_id,
            category='runtime',
            **_build_runtime_chain_diag(
                rt,
                state,
                incoming_history_len=len(incoming_history),
                history_advanced=bool(incoming_history) and incoming_history != history_before[-len(incoming_history):] if incoming_history else False,
                next_bet_amount=next_bet_amount_snapshot,
            ),
        )

    if not rt.get("switch", True):
        if rt.get("bet", False):
            rt["bet"] = False
            user_ctx.save_state()
        return

    if rt.get("manual_pause", False):
        if rt.get("bet", False):
            rt["bet"] = False
            user_ctx.save_state()
        return

    open_bet_entry = _get_latest_open_bet_entry(state)
    if rt.get("bet", False) and open_bet_entry is not None:
        history_delta = _infer_history_advance_result(history_before, incoming_history)
        history_advanced = bool(history_delta.get("advanced", False))
        if history_advanced:
            pre_heal_diag = _build_runtime_chain_diag(
                rt,
                state,
                heal_reason="history_advanced_with_open_bet",
                history_before_len=len(history_before),
                incoming_history_len=len(incoming_history),
                history_delta_mode=history_delta.get("mode", ""),
                history_delta_shift=int(history_delta.get("shift", 0) or 0),
                inferred_result=history_delta.get("result", None),
            )
            inferred_result = history_delta.get("result", None)
            if inferred_result in (0, 1):
                inferred = _apply_inferred_settle_from_history(state, rt, open_bet_entry, int(inferred_result))
                log_event(
                    logging.WARNING,
                    'bet_on',
                    '运行中按历史推断补结算',
                    user_id=user_ctx.user_id,
                    category='warning',
                    **{
                        **pre_heal_diag,
                        "healed_bet_id": str(open_bet_entry.get("bet_id", "unknown")),
                        "inferred_outcome": inferred.get("result_text", ""),
                        "inferred_profit": int(inferred.get("profit", 0) or 0),
                        "chain_sequence_after": int(inferred.get("sequence_after", 0) or 0),
                        "chain_lose_after": int(inferred.get("lose_count_after", 0) or 0),
                        "next_bet_amount_after": int(inferred.get("next_bet_amount", 0) or 0),
                    },
                )
                user_ctx.save_state()
                await _send_transient_admin_notice(
                    client,
                    user_ctx,
                    global_config,
                    _build_ops_card(
                        "🩹 运行中已按历史补结算",
                        summary="检测到上一手疑似漏结算，系统已根据新一轮历史推断该手结果并自动对齐。",
                        fields=[
                            ("补结算记录", str(open_bet_entry.get("bet_id", "unknown"))),
                            ("推断结果", inferred.get("result_text", "")),
                            ("当前连续押注", f"{inferred.get('sequence_after', 0)} 次"),
                            ("当前连输", f"{inferred.get('lose_count_after', 0)} 次"),
                            ("下一手预计下注", _format_money_message(inferred.get("next_bet_amount", 0))),
                        ],
                        action="建议执行 `status` 复核当前链路；本局会继续尝试正常下注。",
                    ),
                    ttl_seconds=180,
                    attr_name="pending_bet_heal_message",
                    msg_type="skip_notice",
                )
            else:
                healed_bet_id = _heal_runtime_open_bet(open_bet_entry, rt)
                summary = reconcile_bet_runtime_from_log(user_ctx)
                log_event(
                    logging.WARNING,
                    'bet_on',
                    '运行中自愈漏结算挂单',
                    user_id=user_ctx.user_id,
                    category='warning',
                    **{
                        **pre_heal_diag,
                        "healed_bet_id": healed_bet_id,
                        "reconciled_sequence": int(summary.get("continuous_count", 0) or 0),
                        "reconciled_lose_count": int(summary.get("lose_count", 0) or 0),
                        "reconciled_bet_amount": int(rt.get("bet_amount", 0) or 0),
                        "reconciled_next_bet_amount": int(calculate_bet_amount(rt) or 0),
                    },
                )
                user_ctx.save_state()
                await _send_transient_admin_notice(
                    client,
                    user_ctx,
                    global_config,
                    _build_ops_card(
                        "🩹 运行中已修正异常挂单",
                        summary="检测到上一手疑似漏结算，但无法可靠推断结果，系统先按保守方式对齐。",
                        fields=[
                            ("修复记录", healed_bet_id),
                            ("当前连续押注", f"{summary.get('continuous_count', 0)} 次"),
                            ("当前连输", f"{summary.get('lose_count', 0)} 次"),
                        ],
                        action="建议执行 `status` 核对链路；若再次出现，请回传 runtime.log。",
                    ),
                    ttl_seconds=180,
                    attr_name="pending_bet_heal_message",
                    msg_type="skip_notice",
                )
        else:
            log_event(
                logging.WARNING,
                'bet_on',
                '上一手待结算，阻止重复下注',
                user_id=user_ctx.user_id,
                category='warning',
                **_build_runtime_chain_diag(
                    rt,
                    state,
                    hold_reason="open_bet_still_pending",
                    incoming_history_len=len(incoming_history),
                ),
            )
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                _build_ops_card(
                    "⏳ 上一手仍待结算",
                    summary="当前检测到上一手还未完成结算，系统不会重复下注。",
                    fields=[("待结算记录", str(open_bet_entry.get("bet_id", "unknown")))],
                    action="建议等待结果回写；若长时间不更新，可执行 `status` 检查。",
                ),
                ttl_seconds=90,
                attr_name="pending_bet_hold_message",
                msg_type="skip_notice",
            )
            return
    if rt.get("bet", False) and open_bet_entry is None:
        rt["bet"] = False
        user_ctx.save_state()

    healed_pending = heal_stale_pending_bets(user_ctx)
    if healed_pending.get("count", 0) > 0:
        summary = reconcile_bet_runtime_from_log(user_ctx)
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            build_pending_bet_heal_notice(healed_pending, summary, rt),
            ttl_seconds=180,
            attr_name="pending_bet_heal_message",
        )

    stop_count = int(rt.get("stop_count", 0) or 0)
    if stop_count > 0:
        if _verbose_runtime_diag_enabled():
            log_event(
                logging.INFO,
                'bet_on',
                '暂停倒计时阻止下注',
                user_id=user_ctx.user_id,
                category='runtime',
                **_build_runtime_chain_diag(rt, state, next_bet_amount=next_bet_amount_snapshot),
            )
        rt["stop_count"] = max(0, stop_count - 1)
        rt["bet"] = False
        if rt.get("pause_countdown_active", False):
            rt["pause_countdown_last_remaining"] = int(rt["stop_count"])
            if rt["stop_count"] > 0:
                await _refresh_pause_countdown_notice(
                    client,
                    user_ctx,
                    global_config,
                    remaining_rounds=int(rt["stop_count"]),
                )
        if rt["stop_count"] == 0:
            rt["flag"] = True
            if rt.get("model_pause_active", False):
                _enter_pause(rt, MODEL_FALLBACK_PAUSE_ROUNDS, "模型连续兜底暂停")
                rt["bet_on"] = False
                rt["mode_stop"] = True
                _ensure_model_probe_loop(client, user_ctx, global_config)
            else:
                rt["bet_on"] = True
                rt["mode_stop"] = True
                rt["pause_reason"] = ""
                await _clear_pause_countdown_notice(client, user_ctx)
        user_ctx.save_state()
        return

    bet_amount = calculate_bet_amount(rt)
    if bet_amount <= 0:
        if not rt.get("limit_stop_notified", False):
            lose_stop = int(rt.get("lose_stop", 13))
            lose_count = int(rt.get("lose_count", 0))
            mes = (
                "⚠️ 已达到预设连投上限，已自动暂停\n"
                f"当前预设最多连投：{lose_stop} 手\n"
                f"当前连输：{lose_count} 手\n"
                "等待 10 局后将用首注金额重新开始"
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            rt["limit_stop_notified"] = True
            
            # 设置暂停 10 局，并从首注重新开始
            rt["stop_count"] = 10
            rt["bet_sequence_count"] = 0
            rt["bet_amount"] = int(rt.get("initial_amount", 500))
            rt["lose_count"] = 0
            rt["win_count"] = 0
            rt["earnings"] = rt.get("earnings", 0) + profit if 'profit' in locals() else rt.get("earnings", 0)
            
            _enter_pause(rt, 10, "连输止损暂停，10 局后重置首注")
            log_event(
                logging.INFO,
                'bet_on',
                '连输止损已暂停，10 局后重置',
                user_id=user_ctx.user_id,
                data=f"lose_count={lose_count}, 将在 10 局后用首注 {int(rt.get('initial_amount', 500))} 重新开始"
            )
        
        log_event(
            logging.WARNING,
            'bet_on',
            '达到连投上限，停止下注',
            user_id=user_ctx.user_id,
            category='warning',
            **_build_runtime_chain_diag(rt, state, lose_stop=lose_stop, next_bet_amount=bet_amount),
        )
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        user_ctx.save_state()
        return
    rt["limit_stop_notified"] = False

    if not is_fund_available(user_ctx, bet_amount):
        if not rt.get("fund_pause_notified", False):
            display_fund = max(0, rt.get("gambling_fund", 0))
            mes = _build_fund_pause_message(display_fund)
            await send_message_v2(
                client,
                "fund_pause",
                mes,
                user_ctx,
                global_config,
                title=f"菠菜机器人 {user_ctx.config.name} 资金暂停",
                desp=mes,
            )
            rt["fund_pause_notified"] = True
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        user_ctx.save_state()
        return
    rt["fund_pause_notified"] = False

    if not (rt.get("bet_on", False) or rt.get("mode_stop", True)):
        return

    if not event.reply_markup:
        _emit_bet_timing("no_markup")
        rt["bet"] = False
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            _build_ops_card(
                "⏭️ 本局未执行下注",
                summary="当前盘口消息没有可点击按钮，系统已自动跳过本局。",
                action="建议等待下一次盘口；如频繁出现，请检查群消息格式。",
            ),
            ttl_seconds=90,
            attr_name="skip_reason_message",
            msg_type="skip_notice",
        )
        return

    next_sequence = int(rt.get("bet_sequence_count", 0) or 0) + 1
    history_signature = "".join(str(x) for x in state.history[-12:])
    history = state.history
    
    # 简单跟随策略：跟随上一手结果下注
    # 检测 6 位纯交替模式（10101 或 01010）
    log_event(logging.INFO, 'bet_on', '策略诊断', user_id=user_ctx.user_id, 
              data=f"历史：{history[-10:]}, 最后一手：{history[-1] if history else '无'}")
    
    if len(history) >= 5:
        last_5 = "".join(str(x) for x in history[-5:])
        if last_5 in ("10101", "01010"):
            # 6 位纯交替，下注与上一手相反（打破交替）
            prediction = 1 - history[-1]
            rt["last_predict_source"] = "alternation_break"
            rt["last_predict_tag"] = "ALTERNATION_BREAK"
            rt["last_predict_confidence"] = 100
            rt["last_predict_reason"] = f"6 位纯交替{last_5}，反向下注{'大' if prediction == 1 else '小'}"
            log_event(logging.INFO, 'bet_on', '交替打破', user_id=user_ctx.user_id,
                      data=f"last_5={last_5}, history[-1]={history[-1]}, prediction={prediction}")
        else:
            # 正常跟随上一手
            prediction = history[-1]
            rt["last_predict_source"] = "follow_last"
            rt["last_predict_tag"] = "FOLLOW_TREND"
            rt["last_predict_confidence"] = 50
            rt["last_predict_reason"] = f"跟随上一手{history[-1]}，下{'大' if prediction == 1 else '小'}"
            log_event(logging.INFO, 'bet_on', '跟随策略', user_id=user_ctx.user_id,
                      data=f"history[-1]={history[-1]}, prediction={prediction}")
    elif len(history) > 0:
        # 历史不足 5 手，直接跟随最后一手
        prediction = history[-1]
        rt["last_predict_source"] = "follow_last"
        rt["last_predict_tag"] = "FOLLOW_TREND"
        rt["last_predict_confidence"] = 50
        rt["last_predict_reason"] = f"跟随上一手{history[-1]}，下{'大' if prediction == 1 else '小'}"
    else:
        # 没有历史数据，默认下大
        prediction = 1
        rt["last_predict_source"] = "default"
        rt["last_predict_tag"] = "DEFAULT"
        rt["last_predict_confidence"] = 50
        rt["last_predict_reason"] = "无历史数据，默认下大"
    
    rt["last_predict_info"] = f"预测方向：{'大' if prediction == 1 else '小'} - {rt['last_predict_reason']}"
    log_event(logging.INFO, 'bet_on', '最终预测', user_id=user_ctx.user_id, 
              data=f"prediction={prediction} ({'大' if prediction == 1 else '小'})")
    
    # 简单策略总是执行下注，直接进入下注执行流程
    
    await _notify_ai_key_warning_if_needed(client, user_ctx, global_config)

    planned_bet_amount = int(bet_amount)
    direction = "大" if prediction == 1 else "小"
    direction_en = "big" if prediction == 1 else "small"
    buttons = constants.BIG_BUTTON if prediction == 1 else constants.SMALL_BUTTON
    combination = constants.find_combination(planned_bet_amount, buttons)

    if not combination:
        log_event(
            logging.WARNING,
            'bet_on',
            '目标金额缺少可点击按钮组合',
            user_id=user_ctx.user_id,
            category='warning',
            **_build_runtime_chain_diag(
                rt,
                state,
                next_bet_amount=planned_bet_amount,
                button_side=direction_en,
            ),
        )
        rt["bet"] = False
        user_ctx.save_state()
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
                _build_ops_card(
                    "⏭️ 本局未执行下注",
                    summary="当前金额没有匹配到可点击的下注按钮组合。",
                    fields=[("目标金额", _format_money_message(planned_bet_amount))],
                ),
            ttl_seconds=120,
            attr_name="skip_reason_message",
            msg_type="skip_notice",
        )
        return

    effective_click_timeout_sec = _resolve_click_timeout_sec(click_timeout_sec, len(combination))
    try:
        click_started_at = time.monotonic()
        for amount in combination:
            button_data = buttons.get(amount)
            if button_data is not None:
                await asyncio.wait_for(
                    _click_bet_button_with_recover(client, event, user_ctx, button_data),
                    timeout=effective_click_timeout_sec,
                )
                await asyncio.sleep(click_interval_sec)
        click_ms = int((time.monotonic() - click_started_at) * 1000)
    except Exception as e:
        click_ms = int((time.monotonic() - click_started_at) * 1000)
        _emit_bet_timing(
            "click_failed",
            timing_sequence=int(next_sequence),
            timing_predict_source=str(rt.get("last_predict_source", "")),
            timing_error=str(e)[:120],
        )
        rt["bet"] = False
        user_ctx.save_state()
        if isinstance(e, asyncio.TimeoutError):
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                _build_ops_card(
                    "⏰ 本轮下注响应超时",
                    summary="当前盘口按钮响应过慢，本次下注未完成，当前倍投链不会推进。",
                    fields=[
                        ("目标金额", _format_money_message(planned_bet_amount)),
                        ("按钮数量", len(combination)),
                    ],
                ),
                ttl_seconds=120,
                attr_name="bet_execute_error_message",
                msg_type="skip_notice",
            )
        elif _is_invalid_callback_message_error(e) or "下注窗口失效" in str(e):
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                _build_ops_card(
                    "⏰ 本轮下注窗口已失效",
                    summary="当前盘口按钮已经不可用，系统已自动跳过本局，当前倍投链不会推进。",
                    fields=[("目标金额", _format_money_message(planned_bet_amount))],
                ),
                ttl_seconds=120,
                attr_name="bet_execute_error_message",
                msg_type="skip_notice",
            )
        else:
            await _send_transient_admin_notice(
                client,
                user_ctx,
                global_config,
                _build_ops_card(
                    "❌ 押注执行失败",
                    summary="本次下注没有执行成功。",
                    fields=[
                        ("目标金额", _format_money_message(planned_bet_amount)),
                        ("错误", str(e)[:180] or "未知错误"),
                    ],
                ),
                ttl_seconds=120,
                attr_name="bet_execute_error_message",
                msg_type="skip_notice",
            )
        return

    rt["bet_amount"] = planned_bet_amount
    rt["bet"] = True
    rt["total"] = rt.get("total", 0) + 1
    rt["bet_sequence_count"] = rt.get("bet_sequence_count", 0) + 1
    rt["bet_type"] = 1 if prediction == 1 else 0
    rt["bet_on"] = True
    rt["fund_pause_notified"] = False
    rt["limit_stop_notified"] = False

    bet_id = generate_bet_id(user_ctx)
    _append_bet_sequence_entry(state, {
        "bet_id": bet_id,
        "sequence": rt.get("bet_sequence_count", 0),
        "direction": direction_en,
        "amount": rt["bet_amount"],
        "result": None,
        "profit": 0,
        "lose_stop": rt.get("lose_stop", 13),
        "profit_target": rt.get("profit", 1000000)
    })
    _clear_hand_stall_guard(rt)

    log_event(
        logging.INFO,
        'bet_on',
        '下注执行完成',
        user_id=user_ctx.user_id,
        category='business',
        **_build_runtime_chain_diag(
            rt,
            state,
            placed_bet_id=bet_id,
            placed_direction=direction_en,
            placed_amount=rt["bet_amount"],
            predict_source=str(rt.get("last_predict_source", "")),
            predict_tag=str(rt.get("last_predict_tag", "")),
            predict_confidence=int(rt.get("last_predict_confidence", 0) or 0),
        ),
    )
    _emit_bet_timing(
        "placed",
        timing_sequence=int(rt.get("bet_sequence_count", 0) or 0),
        timing_predict_source=str(rt.get("last_predict_source", "")),
        timing_predict_tag=str(rt.get("last_predict_tag", "")),
        timing_predict_confidence=int(rt.get("last_predict_confidence", 0) or 0),
        timing_amount=int(rt.get("bet_amount", 0) or 0),
    )

    bet_report = generate_mobile_bet_report(
        state.history,
        direction,
        rt["bet_amount"],
        rt.get("bet_sequence_count", 1),
        bet_id
    )
    message = await send_to_admin(client, bet_report, user_ctx, global_config)
    asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
    if message:
        asyncio.create_task(delete_later(client, message.chat_id, message.id, 100))

    await _refresh_dashboard_message_slim(client, user_ctx, global_config)

    rt["current_bet_seq"] = int(rt.get("current_bet_seq", 1)) + 1
    user_ctx.save_state()


async def process_bet_on(client, event, user_ctx: UserContext, global_config: dict):
    return await _process_bet_on_slim(client, event, user_ctx, global_config)


async def cleanup_message(client, message_ref):
    """安全地删除指定消息对象。"""
    if not message_ref:
        return
    if getattr(message_ref, "is_bot_api", False):
        bot_token = getattr(message_ref, "bot_token", "")
        chat_id = getattr(message_ref, "chat_id", None)
        msg_id = getattr(message_ref, "id", None)
        if bot_token and chat_id is not None and msg_id is not None:
            try:
                url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
                await _post_json_async(url, {"chat_id": chat_id, "message_id": msg_id}, timeout=5)
                return
            except Exception:
                pass
    try:
        await message_ref.delete()
        return
    except Exception:
        pass
    try:
        chat_id = getattr(message_ref, "chat_id", None)
        msg_id = getattr(message_ref, "id", None)
        if chat_id is not None and msg_id is not None:
            await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def process_red_packet(client, event, user_ctx: UserContext, global_config: dict):
    """处理红包消息，尝试领取。"""
    sender_id = getattr(event, "sender_id", None)
    zq_bot = user_ctx.config.groups.get("zq_bot")
    zq_bot_targets = {str(item) for item in _iter_targets(zq_bot)}
    if zq_bot_targets and str(sender_id) not in zq_bot_targets:
        return

    text = (getattr(event, "raw_text", None) or getattr(event, "text", None) or "").strip()
    if not text:
        return

    reply_markup = getattr(event, "reply_markup", None)
    rows = getattr(reply_markup, "rows", None) if reply_markup else None
    if not rows:
        return

    red_keywords = ("红包", "领取", "抢红包", "red", "packet", "hongbao", "claim")
    game_keywords = ("游戏", "对战", "闯关", "开局", "竞猜", "匹配", "挑战", "start game")
    lower_text = text.lower()

    callback_buttons = []
    red_button_candidates = []
    for row_idx, row in enumerate(rows):
        for btn_idx, btn in enumerate(getattr(row, "buttons", None) or []):
            btn_data = getattr(btn, "data", None)
            if not btn_data:
                continue
            btn_text = str(getattr(btn, "text", "") or "")
            try:
                data_text = btn_data.decode("utf-8", errors="ignore") if isinstance(btn_data, (bytes, bytearray)) else str(btn_data)
            except Exception:
                data_text = str(btn_data)

            text_l = btn_text.lower()
            data_l = data_text.lower()
            callback_buttons.append((row_idx, btn_idx, btn_data, text_l, data_l))

            if any(k in text_l for k in red_keywords) or any(k in data_l for k in red_keywords):
                red_button_candidates.append((row_idx, btn_idx, btn_data, text_l, data_l))

    if not callback_buttons:
        return

    has_red_text = ("灵石" in text and "红包" in text) or any(k in lower_text for k in ("抢红包", "领取红包"))
    has_game_hint = any(k in lower_text for k in game_keywords)

    # 仅处理明确红包消息；若是游戏提示且没有红包信号，直接忽略
    if not has_red_text and not red_button_candidates:
        return
    if has_game_hint and not has_red_text and not red_button_candidates:
        return

    # 优先红包候选按钮，否则回退第一个可点击按钮（兼容旧脚本）
    target_row_idx, target_btn_idx, button_data, _, _ = (
        red_button_candidates[0] if red_button_candidates else callback_buttons[0]
    )

    log_event(
        logging.INFO,
        "red_packet",
        "检测到红包按钮消息",
        user_id=user_ctx.user_id,
        msg_id=getattr(event, "id", None),
    )

    from telethon.tl import functions as tl_functions
    import re

    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            try:
                await event.click(target_row_idx, target_btn_idx)
            except Exception:
                await event.click(button_data)
            await asyncio.sleep(1)

            response = await client(
                tl_functions.messages.GetBotCallbackAnswerRequest(
                    peer=event.chat_id,
                    msg_id=event.id,
                    data=button_data,
                )
            )
            response_msg = getattr(response, "message", "") or ""

            if "已获得" in response_msg:
                bonus_match = re.search(r"已获得\s*(\d+)\s*灵石", response_msg)
                bonus = bonus_match.group(1) if bonus_match else "未知数量"
                mes = f"🎉 抢到红包{bonus}灵石！"
                log_event(
                    logging.INFO,
                    "red_packet",
                    "领取成功",
                    user_id=user_ctx.user_id,
                    bonus=bonus,
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                return

            if any(flag in response_msg for flag in ("不能重复领取", "来晚了", "领过")):
                mes = "🧧 红包领取失败 🧧\n\n原因：来晚了，红包已被领完"
                log_event(
                    logging.INFO,
                    "red_packet",
                    "红包已领取或过期",
                    user_id=user_ctx.user_id,
                    response=response_msg,
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                return

            log_event(
                logging.WARNING,
                "red_packet",
                "红包领取回复未知，准备重试",
                user_id=user_ctx.user_id,
                attempt=attempt + 1,
                response=response_msg[:80],
            )
        except Exception as e:
            log_event(
                logging.WARNING,
                "red_packet",
                "尝试领取红包失败",
                user_id=user_ctx.user_id,
                attempt=attempt + 1,
                error=str(e),
            )

        if attempt < max_attempts - 1:
            await asyncio.sleep(1)

    log_event(
        logging.WARNING,
        "red_packet",
        "多次尝试后未成功领取红包",
        user_id=user_ctx.user_id,
        msg_id=getattr(event, "id", None),
    )


def is_fund_available(user_ctx: UserContext, bet_amount: int = 0) -> bool:
    """检查资金是否充足（与 master 版语义一致：需同时满足余额>0且>=本次下注金额）。"""
    rt = user_ctx.state.runtime
    gambling_fund = rt.get("gambling_fund", 0)
    return gambling_fund > 0 and gambling_fund >= bet_amount


def _is_invalid_callback_message_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "message id is invalid",
        "getbotcallbackanswerrequest",
        "can't do that operation on such message",
        "messageidinvaliderror",
    )
    return any(marker in text for marker in markers)


async def _find_latest_bet_prompt_message(client, event, user_ctx: UserContext):
    """回溯最近可点击的下注提示消息，用于 message id 失效时恢复。"""
    zq_bot = user_ctx.config.groups.get("zq_bot")
    zq_bot_targets = {str(item) for item in _iter_targets(zq_bot)}
    hints = ("[近 40 次结果]", "由近及远", "0 小 1 大")

    try:
        async for msg in client.iter_messages(event.chat_id, limit=20):
            if zq_bot_targets and str(getattr(msg, "sender_id", None)) not in zq_bot_targets:
                continue
            if not getattr(msg, "reply_markup", None):
                continue
            raw = (getattr(msg, "message", None) or getattr(msg, "raw_text", None) or "").strip()
            if any(hint in raw for hint in hints):
                return msg
    except Exception as e:
        log_event(logging.DEBUG, "bet_on", "回溯下注提示消息失败", user_id=user_ctx.user_id, error=str(e))
    return None


async def _click_bet_button_with_recover(client, event, user_ctx: UserContext, button_data):
    """点击下注按钮；若原消息失效，则回溯最新下注提示消息重试。"""
    try:
        await event.click(button_data)
        return
    except Exception as e:
        if not _is_invalid_callback_message_error(e):
            raise

    latest_msg = await _find_latest_bet_prompt_message(client, event, user_ctx)
    if latest_msg is None:
        raise RuntimeError("下注窗口失效且未找到可用的最新下注消息")

    await latest_msg.click(button_data)
    log_event(
        logging.WARNING,
        "bet_on",
        "原下注消息失效，已使用最新消息重试按钮点击",
        user_id=user_ctx.user_id,
        src_msg=getattr(event, "id", None),
        retry_msg=getattr(latest_msg, "id", None),
    )


def _read_timing_config(global_config: dict) -> dict:
    """读取下注时序参数，提供安全兜底。"""
    cfg = global_config.get("betting") if isinstance(global_config.get("betting"), dict) else {}

    def _to_float(name: str, default: float, minimum: float, maximum: float) -> float:
        raw = cfg.get(name, default)
        try:
            val = float(raw)
        except Exception:
            return default
        return max(minimum, min(maximum, val))

    return {
        "prompt_wait_sec": _to_float("prompt_wait_sec", 1.2, 0.0, 5.0),
        "predict_timeout_sec": _to_float("predict_timeout_sec", 8.0, 1.0, 30.0),
        "click_interval_sec": _to_float("click_interval_sec", 0.45, 0.05, 2.0),
        "click_timeout_sec": _to_float("click_timeout_sec", 6.0, 1.0, 20.0),
    }


def _resolve_click_timeout_sec(base_timeout_sec: float, combination_len: int) -> float:
    """高金额多按钮组合给予更宽松的单次点击超时，避免误判点击失败。"""
    try:
        combo_len = int(combination_len or 0)
    except Exception:
        combo_len = 0
    timeout_sec = float(base_timeout_sec or 0)
    if combo_len >= 10:
        return max(timeout_sec, 7.0)
    if combo_len >= 8:
        return max(timeout_sec, 6.0)
    return timeout_sec


def calculate_bet_amount(rt: dict) -> int:
    """按 master 逻辑计算本局下注金额。"""
    win_count = rt.get("win_count", 0)
    lose_count = rt.get("lose_count", 0)
    initial_amount = int(rt.get("initial_amount", 500))
    lose_stop = int(rt.get("lose_stop", 13))
    lose_once = float(rt.get("lose_once", 3))
    lose_twice = float(rt.get("lose_twice", 2.1))
    lose_three = float(rt.get("lose_three", 2.1))
    lose_four = float(rt.get("lose_four", 2.05))

    if win_count >= 0 and lose_count == 0:
        return constants.closest_multiple_of_500(initial_amount)

    if (lose_count + 1) > lose_stop:
        return 0

    base_amount = int(rt.get("bet_amount", initial_amount))
    if lose_count == 1:
        target = base_amount * lose_once
    elif lose_count == 2:
        target = base_amount * lose_twice
    elif lose_count == 3:
        target = base_amount * lose_three
    else:
        target = base_amount * lose_four

    # 与 master 一致：补 1% 安全边际
    return constants.closest_multiple_of_500(target + target * 0.01)


def _build_pause_resume_hint(rt: dict) -> str:
    """构建“暂停结束后会做什么”的提示。"""
    next_sequence = int(rt.get("bet_sequence_count", 0)) + 1
    next_amount = int(calculate_bet_amount(rt) or 0)
    if next_amount > 0:
        return f"恢复后动作：继续第 {next_sequence} 手，预计下注 {_format_money_message(next_amount)}"
    return f"恢复后动作：继续第 {next_sequence} 手"


def _format_predict_signal_brief(rt: dict) -> str:
    """把模型信号整理成易读短句，用于暂停恢复提示。"""
    source = str(rt.get("last_predict_source", "unknown") or "unknown")
    tag = str(rt.get("last_predict_tag", "") or "UNKNOWN")
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    reason = str(rt.get("last_predict_reason", "") or "").strip()
    if reason:
        return f"来源 {source} | 标签 {tag} | 置信度 {confidence}% | 理由 {reason}"
    return f"来源 {source} | 标签 {tag} | 置信度 {confidence}%"


def _get_history_tail_streak(history: list) -> tuple:
    """返回历史尾部连庄信息：(连庄长度, 连庄方向0/1)。"""
    if not isinstance(history, list) or not history:
        return 0, -1
    try:
        tail_value = int(history[-1])
    except Exception:
        return 0, -1
    streak = 1
    for idx in range(len(history) - 2, -1, -1):
        try:
            current = int(history[idx])
        except Exception:
            break
        if current != tail_value:
            break
        streak += 1
    return streak, tail_value


def _should_skip_repeated_entry_timeout_gate(rt: dict, next_sequence: int, settled_count: int) -> bool:
    """
    防止同一连押阶段在“无新结算”的情况下，因模型持续超时而反复触发暂停。
    仅用于“模型可用性门控（超时）”去重。
    """
    last_seq_raw = rt.get("entry_timeout_gate_last_seq", -1)
    last_settled_raw = rt.get("entry_timeout_gate_last_settled", -1)
    try:
        last_seq = int(last_seq_raw)
    except Exception:
        last_seq = -1
    try:
        last_settled = int(last_settled_raw)
    except Exception:
        last_settled = -1
    if last_seq == int(next_sequence) and last_settled == int(settled_count):
        return True
    rt["entry_timeout_gate_last_seq"] = int(next_sequence)
    rt["entry_timeout_gate_last_settled"] = int(settled_count)
    return False


def _clear_hand_stall_guard(rt: dict) -> None:
    """清理“同手位卡死防护”计数器。"""
    rt["stall_guard_sequence"] = -1
    rt["stall_guard_last_history_len"] = -1
    rt["stall_guard_last_history_sig"] = ""
    rt["stall_guard_no_bet_streak"] = 0
    rt["stall_guard_skip_streak"] = 0
    rt["stall_guard_timeout_streak"] = 0
    rt["stall_guard_gate_streak"] = 0
    rt["stall_guard_force_unlock_used"] = False


def _record_hand_stall_block(rt: dict, next_sequence: int, history_signature: str, reason: str) -> dict:
    """
    记录同手位“未下单”阻断事件，并判断是否触发观望解锁或观望暂停。
    当前主要服务于模型主动观望（skip）。
    """
    reason = str(reason or "gate").strip().lower()
    if reason not in {"skip", "timeout", "gate"}:
        reason = "gate"

    current_seq = int(rt.get("stall_guard_sequence", -1))
    if current_seq != int(next_sequence):
        _clear_hand_stall_guard(rt)
        rt["stall_guard_sequence"] = int(next_sequence)

    current_signature = str(history_signature or "").strip()
    last_signature = str(rt.get("stall_guard_last_history_sig", "") or "").strip()
    if current_signature and current_signature != last_signature:
        rt["stall_guard_last_history_sig"] = current_signature
        rt["stall_guard_last_history_len"] = len(current_signature)
        rt["stall_guard_no_bet_streak"] = int(rt.get("stall_guard_no_bet_streak", 0)) + 1
        if reason == "skip":
            rt["stall_guard_skip_streak"] = int(rt.get("stall_guard_skip_streak", 0)) + 1
        elif reason == "timeout":
            rt["stall_guard_timeout_streak"] = int(rt.get("stall_guard_timeout_streak", 0)) + 1
        else:
            rt["stall_guard_gate_streak"] = int(rt.get("stall_guard_gate_streak", 0)) + 1

    no_bet_streak = int(rt.get("stall_guard_no_bet_streak", 0))
    skip_streak = int(rt.get("stall_guard_skip_streak", 0))
    timeout_streak = int(rt.get("stall_guard_timeout_streak", 0))
    gate_streak = int(rt.get("stall_guard_gate_streak", 0))

    unlock_used = bool(rt.get("stall_guard_force_unlock_used", False))
    force_unlock = False
    pause_rounds = 0
    pause_reason = ""

    if reason == "skip":
        if int(next_sequence) <= STALL_GUARD_LOW_STEP_UNLOCK_MAX:
            if skip_streak > STALL_GUARD_SKIP_MAX and not unlock_used:
                force_unlock = True
            elif skip_streak > STALL_GUARD_SKIP_MAX and unlock_used:
                pause_rounds = STALL_GUARD_HIGH_STEP_PAUSE_ROUNDS
                pause_reason = "低手位连续观望暂停"
        elif int(next_sequence) >= STALL_GUARD_HIGH_STEP_MIN and skip_streak >= 2:
            pause_rounds = STALL_GUARD_HIGH_STEP_PAUSE_ROUNDS
            pause_reason = "高手位连续观望暂停"

    return {
        "force_unlock": force_unlock,
        "pause_rounds": pause_rounds,
        "pause_reason": pause_reason,
        "sequence": int(next_sequence),
        "reason": reason,
        "history_signature": current_signature,
        "no_bet_streak": no_bet_streak,
        "skip_streak": skip_streak,
        "timeout_streak": timeout_streak,
        "gate_streak": gate_streak,
        "unlock_used": unlock_used,
    }


def _prepare_force_unlock_prediction(state, rt: dict, next_sequence: int, trigger: dict) -> int:
    """生成防卡死强制解锁预测方向（统计兜底）。"""
    prediction = int(fallback_prediction(state.history))
    rt["last_predict_source"] = "unlock_fallback"
    rt["last_predict_tag"] = "UNLOCK"
    rt["last_predict_confidence"] = 0
    rt["last_predict_reason"] = "低手位连续观望，保守解锁"
    rt["last_predict_info"] = _build_predict_basis_text(
        history=state.history,
        prediction=int(prediction),
        source="unlock_fallback",
        pattern_tag="UNLOCK",
        rhythm_tag="CHAOS_NOISE",
        tail_streak_len=int(rt.get("last_predict_tail_len", 0) or 0),
        tail_streak_char=rt.get("last_predict_tail_char", None),
    )
    rt["stall_guard_force_unlock_total"] = int(rt.get("stall_guard_force_unlock_total", 0)) + 1
    rt["stall_guard_force_unlock_used"] = True
    return prediction


def _select_secondary_model_id(user_ctx: UserContext, primary_model_id: str) -> str:
    """
    从模型链中选择“不同于主模型”的副模型，用于高阶手位二次确认。
    若不存在可用副模型，返回空字符串。
    """
    try:
        model_mgr = user_ctx.get_model_manager()
        primary_cfg = model_mgr.get_model(str(primary_model_id))
        primary_actual = str(primary_cfg.get("model_id")) if primary_cfg else str(primary_model_id)

        ordered_models = []
        chain = list(model_mgr.fallback_chain or [])
        if chain:
            for key in chain:
                cfg = model_mgr.get_model(str(key))
                if not cfg or not cfg.get("enabled", True):
                    continue
                mid = str(cfg.get("model_id", "")).strip()
                if mid and mid not in ordered_models:
                    ordered_models.append(mid)
        else:
            for cfg in model_mgr.models:
                if not cfg.get("enabled", True):
                    continue
                mid = str(cfg.get("model_id", "")).strip()
                if mid and mid not in ordered_models:
                    ordered_models.append(mid)

        if not ordered_models:
            return ""

        if primary_actual in ordered_models:
            idx = ordered_models.index(primary_actual)
            for mid in ordered_models[idx + 1:]:
                if mid != primary_actual:
                    return mid
            for mid in ordered_models[:idx]:
                if mid != primary_actual:
                    return mid
            return ""

        for mid in ordered_models:
            if mid != primary_actual:
                return mid
    except Exception:
        return ""
    return ""


def _is_neutral_long_term_gap(value: float) -> bool:
    try:
        current = float(value)
    except (TypeError, ValueError):
        return False
    return NEUTRAL_LONG_TERM_GAP_LOW <= current <= NEUTRAL_LONG_TERM_GAP_HIGH


def _evaluate_high_pressure_pattern_gate(rt: dict, risk_pause: dict, next_sequence: int) -> dict:
    """
    深度风控开启时的高压位结构门控：
    - 第3手起，不稳定形态需要更高置信度
    - 第5手起，不稳定形态/候选长龙默认从严，优先 SKIP / 暂停
    """
    if next_sequence < 3:
        return {"blocked": False}

    source = str(rt.get("last_predict_source", "unknown")).lower().strip()
    if source != "model":
        return {"blocked": False}

    tag = str(rt.get("last_predict_tag", "") or "UNKNOWN").strip().upper()
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    tail_len = int(rt.get("last_predict_tail_len", 0) or 0)
    long_term_gap = float(rt.get("last_predict_long_term_gap", 0.5) or 0.5)
    wins = int(risk_pause.get("wins", 0))
    total = int(risk_pause.get("total", 0))
    win_rate = (wins / total) if total > 0 else 0.0

    reasons = []
    pause_rounds = 1 if next_sequence < HIGH_PRESSURE_SKIP_MIN_STEP else HIGH_PRESSURE_PATTERN_PAUSE_ROUNDS
    gate_name = "高压位结构门控"

    if tag in UNSTABLE_PATTERN_TAGS:
        conf_threshold = UNSTABLE_PATTERN_MIN_CONF_STEP3 if next_sequence < HIGH_PRESSURE_SKIP_MIN_STEP else UNSTABLE_PATTERN_MIN_CONF_STEP5
        if confidence < conf_threshold:
            reasons.append(f"不稳定形态 {tag} 置信度仅 {confidence}% < {conf_threshold}%")
        if tail_len < 4:
            reasons.append(f"尾部连数仅 {tail_len}，形态未成熟")
        if _is_neutral_long_term_gap(long_term_gap):
            reasons.append(f"长期100局占比 {long_term_gap:.2f} 接近均衡，不能作为下注证据")
        if next_sequence >= HIGH_PRESSURE_SKIP_MIN_STEP:
            reasons.append(f"第{HIGH_PRESSURE_SKIP_MIN_STEP}手及以上不接受 {tag} 直接下注")
    elif tag == "DRAGON_CANDIDATE" and next_sequence >= HIGH_PRESSURE_SKIP_MIN_STEP:
        if tail_len < DRAGON_CANDIDATE_MIN_TAIL_STEP5:
            reasons.append(f"DRAGON_CANDIDATE 尾部连数仅 {tail_len} < {DRAGON_CANDIDATE_MIN_TAIL_STEP5}")
        if confidence < HIGH_PRESSURE_SKIP_MIN_CONF:
            reasons.append(f"候选长龙置信度仅 {confidence}% < {HIGH_PRESSURE_SKIP_MIN_CONF}%")
        if _is_neutral_long_term_gap(long_term_gap):
            reasons.append(f"长期100局占比 {long_term_gap:.2f} 接近均衡，长龙证据不足")
    elif tag == "LONG_DRAGON" and next_sequence >= HIGH_PRESSURE_SKIP_MIN_STEP:
        if tail_len < DRAGON_CANDIDATE_MIN_TAIL_STEP5:
            reasons.append(f"LONG_DRAGON 尾部连数仅 {tail_len}，成熟度不足")
        if confidence < HIGH_PRESSURE_SKIP_MIN_CONF:
            reasons.append(f"LONG_DRAGON 置信度仅 {confidence}% < {HIGH_PRESSURE_SKIP_MIN_CONF}%")

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": source,
            "tag": tag,
            "confidence": confidence,
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}


async def _evaluate_high_step_double_confirm(
    user_ctx: UserContext,
    risk_pause: dict,
    next_sequence: int,
    primary_prediction: int,
    primary_confidence: int,
) -> dict:
    """
    第5手起执行二次确认：主模型 + 副模型必须同向且置信度达标。
    """
    if next_sequence < HIGH_STEP_DOUBLE_CONFIRM_MIN_STEP:
        return {"blocked": False}

    reasons = []
    gate_name = f"第{HIGH_STEP_DOUBLE_CONFIRM_MIN_STEP}手双模型确认门控"
    pause_rounds = HIGH_STEP_DOUBLE_CONFIRM_PAUSE_ROUNDS
    wins = int(risk_pause.get("wins", 0))
    total = int(risk_pause.get("total", 0))
    win_rate = (wins / total) if total > 0 else 0.0
    primary_model_id = str(user_ctx.state.runtime.get("current_model_id", ""))

    if primary_prediction not in (0, 1):
        reasons.append("主模型未给出可下注方向")
    if int(primary_confidence or 0) < HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF:
        reasons.append(
            f"主模型置信度 {int(primary_confidence or 0)}% < {HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF}%"
        )

    secondary_model_id = _select_secondary_model_id(user_ctx, primary_model_id)
    secondary_confidence = 0
    secondary_prediction = -1
    secondary_source = "none"

    if secondary_model_id:
        history = user_ctx.state.history
        short_term_20 = history[-20:] if len(history) >= 20 else history[:]
        medium_term_50 = history[-50:] if len(history) >= 50 else history[:]
        short_str = "".join("1" if x == 1 else "0" for x in short_term_20)
        medium_str = "".join("1" if x == 1 else "0" for x in medium_term_50)
        pattern = extract_pattern_features(history)
        trend_gap = calculate_trend_gap(history, window=100)
        tail_streak_len = int(pattern.get("tail_streak_len", 0) or 0)
        tail_side = "大" if int(pattern.get("tail_streak_char", 0) or 0) == 1 else "小"
        gap = int(trend_gap.get("gap", 0) or 0)
        main_dir = "大" if primary_prediction == 1 else "小"

        prompt = f"""你是风控复核模型，只输出JSON。
当前处于倍投第{next_sequence}手（高风险手位），请做方向复核：
- 主模型方向：{main_dir}
- 主模型置信度：{int(primary_confidence or 0)}%
- 最近20局：{short_str}
- 最近50局：{medium_str}
- 尾部形态：{pattern.get('pattern_tag', 'UNKNOWN')}（{tail_streak_len}连{tail_side}）
- 缺口：{gap:+d}

只输出JSON：
{{"prediction": -1或0或1, "confidence": 1-100, "reason": "20字内"}}"""

        messages = [
            {"role": "system", "content": "你是高风险入场复核器，只返回JSON。"},
            {"role": "user", "content": prompt},
        ]

        try:
            result = await asyncio.wait_for(
                user_ctx.get_model_manager().call_model(
                    secondary_model_id,
                    messages,
                    temperature=0.0,
                    max_tokens=120,
                ),
                timeout=HIGH_STEP_DOUBLE_CONFIRM_MODEL_TIMEOUT_SEC,
            )
            if not result.get("success"):
                raise RuntimeError(str(result.get("error", "unknown")))
            parsed = parse_analysis_result_insight(
                result.get("content", ""),
                default_prediction=primary_prediction,
            )
            secondary_prediction = int(parsed.get("prediction", -1))
            secondary_confidence = int(parsed.get("confidence", 0) or 0)
            secondary_source = secondary_model_id

            if secondary_prediction != primary_prediction:
                if secondary_prediction == -1:
                    reasons.append("副模型建议观望（SKIP）")
                else:
                    side = "大" if secondary_prediction == 1 else "小"
                    reasons.append(f"副模型方向不一致（副模型={side}）")
            if secondary_confidence < HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF:
                reasons.append(
                    f"副模型置信度 {secondary_confidence}% < {HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF}%"
                )
        except Exception as e:
            secondary_source = "error"
            reasons.append(f"副模型复核失败：{str(e)[:60]}")
    else:
        # 无副模型时，启用更严格单模型兜底，避免高风险手位盲目继续。
        if int(primary_confidence or 0) < (HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF + 5):
            reasons.append(
                f"无副模型时主模型置信度需 >= {HIGH_STEP_DOUBLE_CONFIRM_MIN_CONF + 5}%"
            )
        secondary_source = "single"

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": f"primary={primary_model_id},secondary={secondary_source}",
            "tag": str(user_ctx.state.runtime.get("last_predict_tag", "") or "UNKNOWN"),
            "confidence": int(primary_confidence or 0),
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}

def _evaluate_entry_quality_gate(rt: dict, risk_pause: dict, next_sequence: int) -> dict:
    """
    高倍入场质量门控：
    - 第3手：至少满足最低置信度，避免在弱信号下继续放大
    - 第4手：更严格，且限制标签白名单
    """
    if next_sequence not in (3, 4):
        return {"blocked": False}

    source = str(rt.get("last_predict_source", "unknown")).lower()
    tag = str(rt.get("last_predict_tag", "")).strip().upper()
    confidence = int(rt.get("last_predict_confidence", 0) or 0)
    total = int(risk_pause.get("total", 0))
    wins = int(risk_pause.get("wins", 0))
    win_rate = (wins / total) if total > 0 else 0.0

    reasons = []
    pause_rounds = ENTRY_GUARD_STEP3_PAUSE_ROUNDS
    gate_name = "第3手质量门控"

    if source != "model":
        reasons.append("本局预测未拿到稳定模型结果（超时/异常）")

    if next_sequence == 3:
        if confidence < ENTRY_GUARD_STEP3_MIN_CONF:
            reasons.append(f"置信度 {confidence}% < {ENTRY_GUARD_STEP3_MIN_CONF}%")
    elif next_sequence == 4:
        gate_name = "第4手强风控门控"
        pause_rounds = ENTRY_GUARD_STEP4_PAUSE_ROUNDS
        # 样本不足阶段（<40笔）放宽：仅检查置信度，避免第4手过早频繁拦截。
        step4_conf_threshold = ENTRY_GUARD_STEP4_MIN_CONF if total >= RISK_WINDOW_BETS else ENTRY_GUARD_STEP4_MIN_CONF_EARLY
        if confidence < step4_conf_threshold:
            reasons.append(f"置信度 {confidence}% < {step4_conf_threshold}%")
        # 白名单与胜率检查仅在样本充足后生效。
        if total >= RISK_WINDOW_BETS:
            if tag not in ENTRY_GUARD_STEP4_ALLOWED_TAGS:
                reasons.append(f"标签 {tag or 'UNKNOWN'} 不在白名单")
            if win_rate < 0.45:
                reasons.append(f"最近40笔胜率仅 {wins}/{total}（{win_rate * 100:.1f}%）")

    if reasons:
        return {
            "blocked": True,
            "gate_name": gate_name,
            "pause_rounds": pause_rounds,
            "reason_text": "；".join(reasons),
            "source": source,
            "tag": tag or "UNKNOWN",
            "confidence": confidence,
            "wins": wins,
            "total": total,
            "win_rate": win_rate,
        }
    return {"blocked": False}


async def _apply_entry_gate_pause(
    client,
    user_ctx: UserContext,
    global_config: dict,
    gate: dict,
    next_sequence: int,
) -> None:
    """统一发送高倍入场门控暂停提示。"""
    rt = user_ctx.state.runtime
    pause_rounds = max(1, int(gate.get("pause_rounds", 1)))
    _enter_pause(rt, pause_rounds, gate.get("gate_name", "高倍入场门控"))
    user_ctx.save_state()

    total = int(gate.get("total", 0) or 0)
    wins = int(gate.get("wins", 0) or 0)
    if total > 0:
        wr_text = f"{wins}/{total}（{gate.get('win_rate', 0.0) * 100:.1f}%）"
    else:
        wr_text = "样本不足（N/A）"

    pause_msg = (
        f"⛔ 自动风控暂停 ⛔\n\n"
        f"触发点：第 {next_sequence} 手下注前\n"
        f"触发类型：{gate.get('gate_name', '高倍入场门控')}\n"
        f"当前信号：标签 {gate.get('tag', 'UNKNOWN')} | 置信度 {gate.get('confidence', 0)}% | 来源 {gate.get('source', 'unknown')}\n"
        f"最近胜率：{wr_text}\n"
        f"未通过条件：{gate.get('reason_text', '信号质量不足')}\n"
        f"本次暂停：{pause_rounds} 局\n"
        f"暂停期间：保留当前倍投进度，不会重置首注\n"
        f"{_build_pause_resume_hint(rt)}"
    )

    if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
        await cleanup_message(client, user_ctx.risk_pause_message)
    user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=pause_rounds,
    )

def _get_recent_settled_outcomes(state, window: int = RISK_WINDOW_BETS) -> list:
    """提取最近 N 笔已结算结果（赢=1，输=0）。"""
    if window <= 0:
        return []
    outcomes = []
    for entry in reversed(_get_strategy_bet_sequence_log(state)):
        result = entry.get("result")
        if result == "赢":
            outcomes.append(1)
        elif result == "输":
            outcomes.append(0)
        if len(outcomes) >= window:
            break
    outcomes.reverse()
    return outcomes


def _count_settled_bets(state) -> int:
    """统计已结算押注笔数（赢/输）。"""
    count = 0
    for entry in _get_strategy_bet_sequence_log(state):
        result = entry.get("result")
        if result in ("赢", "输"):
            count += 1
    return count


def _fallback_pause_rounds(level: str, wins: int, total: int, lose_count: int, max_pause: int) -> int:
    """模型不可用时的暂停局数兜底。"""
    max_pause = max(1, int(max_pause))
    if total <= 0:
        return min(1, max_pause)

    win_rate = wins / total
    if str(level).startswith("DEEP"):
        if lose_count >= 9:
            base = 2
        elif lose_count >= 6:
            base = 2
        else:
            base = 3
        return max(1, min(max_pause, base))

    # BASE：根据40局胜率分层
    if win_rate <= 0.30:
        base = 4
    elif win_rate <= 0.35:
        base = 3
    else:
        base = 2
    return max(1, min(max_pause, base))


def _parse_pause_rounds_response(raw_text: str, max_pause: int) -> tuple:
    """解析模型返回的暂停建议，返回 (pause_rounds|None, reason)。"""
    if not raw_text:
        return None, ""

    max_pause = max(1, int(max_pause))
    candidates = [raw_text.strip()]
    # 兼容模型返回前后包裹说明文字的情况
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start:end + 1].strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            pause_raw = data.get("pause_rounds", data.get("pause", data.get("rounds")))
            if pause_raw is None:
                continue
            pause_rounds = int(float(str(pause_raw).strip()))
            pause_rounds = max(1, min(max_pause, pause_rounds))
            reason = str(data.get("reason", "")).strip()
            return pause_rounds, reason
        except Exception:
            continue

    return None, ""


async def _suggest_pause_rounds_by_model(
    user_ctx: UserContext,
    risk_eval: dict,
    max_pause: int,
) -> tuple:
    """调用大模型给出暂停局数建议，失败时自动降级到统计兜底。"""
    state = user_ctx.state
    rt = state.runtime
    current_model_id = rt.get("current_model_id")
    wins = int(risk_eval.get("wins", 0))
    total = int(risk_eval.get("total", 0))
    lose_count = int(risk_eval.get("lose_count", 0))
    level = str(risk_eval.get("level", "BASE"))

    fallback_rounds = _fallback_pause_rounds(level, wins, total, lose_count, max_pause)
    fallback_reason = "模型异常，统计兜底"
    if not current_model_id:
        return fallback_rounds, fallback_reason, "fallback"

    recent_tail = risk_eval.get("recent_outcomes", [])[-12:]
    recent_text = "".join(str(x) for x in recent_tail) if recent_tail else "NA"
    prompt = f"""你是一个只负责风险暂停局数的控制器。必须只输出JSON。

当前风控层级：{risk_eval.get('level_label', level)}
最近{total}笔胜率：{wins}/{total}（{risk_eval.get('win_rate', 0.0) * 100:.1f}%）
当前连输：{lose_count}
下一手：第{risk_eval.get('next_sequence', 1)}手
最近12笔结算(赢1输0)：{recent_text}

请给出暂停建议，范围必须在 1 到 {max_pause} 之间。
输出格式：
{{"pause_rounds": 1-{max_pause}之间整数, "reason": "20字内"}}
"""

    messages = [
        {"role": "system", "content": "你是交易风控引擎，只返回JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]

    try:
        result = await asyncio.wait_for(
            user_ctx.get_model_manager().call_model(current_model_id, messages, temperature=0.0, max_tokens=120),
            timeout=RISK_PAUSE_MODEL_TIMEOUT_SEC,
        )
        if not result.get("success"):
            raise RuntimeError(str(result.get("error", "unknown")))

        rounds, reason = _parse_pause_rounds_response(result.get("content", ""), max_pause=max_pause)
        if rounds is None:
            raise ValueError("pause_rounds parse failed")
        reason = reason or "模型建议"
        return rounds, reason, "model"
    except Exception as e:
        log_event(
            logging.WARNING,
            "risk_pause",
            "风控暂停模型建议失败，使用统计兜底",
            user_id=user_ctx.user_id,
            error=str(e),
            level=level,
        )
        return fallback_rounds, fallback_reason, "fallback"


def _get_deep_triggered_milestones(rt: dict) -> list:
    """读取并规范化已触发的深度风控里程碑。"""
    raw = rt.get("risk_deep_triggered_milestones", [])
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        items = []

    normalized = []
    for item in items:
        try:
            normalized.append(int(item))
        except Exception:
            continue
    return sorted(set(normalized))


def _evaluate_auto_risk_pause(state, rt: dict, next_sequence: int) -> dict:
    """
    评估自动风控状态（基础风控 + 深度风控里程碑）。
    基础风控：最近40笔胜率阈值触发（连续命中由外层控制）
    深度风控：连输每达到 3 的倍数档位时触发（每档同一连输周期仅触发一次）
    """
    outcomes = _get_recent_settled_outcomes(state, RISK_WINDOW_BETS)
    total = len(outcomes)
    wins = int(sum(outcomes))
    win_rate = wins / total if total > 0 else 0.0
    lose_count = int(rt.get("lose_count", 0))
    base_window_ready = total >= RISK_WINDOW_BETS
    base_trigger = base_window_ready and wins <= RISK_BASE_TRIGGER_WINS
    recovery_hit = base_window_ready and wins >= RISK_RECOVERY_WINS

    triggered_milestones = _get_deep_triggered_milestones(rt)
    deep_milestone = 0
    deep_level_cap = 0
    lose_stop = max(1, int(rt.get("lose_stop", 13)))
    if lose_count >= RISK_DEEP_TRIGGER_INTERVAL and lose_count < lose_stop:
        current_milestone = (lose_count // RISK_DEEP_TRIGGER_INTERVAL) * RISK_DEEP_TRIGGER_INTERVAL
        if current_milestone > 0 and current_milestone not in triggered_milestones:
            deep_milestone = current_milestone
            if current_milestone == RISK_DEEP_TRIGGER_INTERVAL:
                deep_level_cap = int(RISK_DEEP_FIRST_MAX_PAUSE_ROUNDS)
            else:
                deep_level_cap = int(RISK_DEEP_NEXT_MAX_PAUSE_ROUNDS)

    reasons = []
    if base_trigger:
        reasons.append("最近40笔胜率<=37.5%")
    if deep_milestone > 0:
        reasons.append(f"连输达到{deep_milestone}局档位（每3局触发）")

    return {
        "triggered": bool(base_trigger or deep_milestone > 0),
        "wins": wins,
        "total": total,
        "win_rate": win_rate,
        "next_sequence": next_sequence,
        "lose_count": lose_count,
        "base_window_ready": base_window_ready,
        "base_trigger": base_trigger,
        "recovery_hit": recovery_hit,
        "deep_trigger": deep_milestone > 0,
        "deep_milestone": deep_milestone,
        "deep_level_cap": deep_level_cap,
        "deep_triggered_milestones": triggered_milestones,
        "reasons": reasons,
        "recent_outcomes": outcomes[-20:],
    }


def _apply_auto_risk_pause(rt: dict, pause_rounds: int) -> None:
    """
    执行自动风控暂停。
    说明：stop_count 在下注入口每轮先减1，设为 (暂停局数+1) 才能真正停满指定局数。
    """
    pause_rounds = max(1, int(pause_rounds))
    internal_stop_count = pause_rounds + 1

    rt["stop_count"] = max(int(rt.get("stop_count", 0)), internal_stop_count)
    rt["bet_on"] = False
    rt["bet"] = False
    rt["mode_stop"] = False


def _enter_pause(rt: dict, pause_rounds: int, reason: str) -> int:
    """
    统一暂停入口：写入暂停状态 + 倒计时上下文。
    返回规范化后的暂停局数。
    """
    rounds = max(1, int(pause_rounds))
    _apply_auto_risk_pause(rt, rounds)
    _set_pause_countdown_context(rt, reason, rounds)
    return rounds


def _set_pause_countdown_context(rt: dict, reason: str, pause_rounds: int) -> None:
    """写入统一暂停倒计时上下文（手动暂停不使用该机制）。"""
    rounds = max(1, int(pause_rounds))
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = str(reason or "自动暂停")
    rt["pause_countdown_total_rounds"] = rounds
    rt["pause_countdown_last_remaining"] = -1
    # 每次进入新暂停周期后，恢复复核提示应重新可发送一次。
    rt["pause_resume_probe_settled"] = -1


async def _clear_pause_countdown_notice(client, user_ctx: UserContext) -> None:
    """清理暂停倒计时消息与上下文。"""
    rt = user_ctx.state.runtime
    if hasattr(user_ctx, "pause_countdown_message") and user_ctx.pause_countdown_message:
        await cleanup_message(client, user_ctx.pause_countdown_message)
        user_ctx.pause_countdown_message = None
    rt["pause_countdown_active"] = False
    rt["pause_countdown_reason"] = ""
    rt["pause_countdown_total_rounds"] = 0
    rt["pause_countdown_last_remaining"] = -1


async def _refresh_pause_countdown_notice(
    client,
    user_ctx: UserContext,
    global_config: dict,
    remaining_rounds: int = None,
) -> None:
    """刷新式推送暂停倒计时通知。"""
    rt = user_ctx.state.runtime
    if rt.get("manual_pause", False):
        return
    if not rt.get("pause_countdown_active", False):
        return

    total_rounds = int(rt.get("pause_countdown_total_rounds", 0))
    if total_rounds <= 0:
        return

    if remaining_rounds is None:
        remaining_rounds = int(rt.get("stop_count", 0))
    remaining_rounds = max(0, min(total_rounds, int(remaining_rounds)))

    if remaining_rounds <= 0:
        return

    last_remaining = int(rt.get("pause_countdown_last_remaining", -1))
    if (
        last_remaining == remaining_rounds
        and hasattr(user_ctx, "pause_countdown_message")
        and user_ctx.pause_countdown_message
    ):
        return

    reason = str(rt.get("pause_countdown_reason", "自动暂停")).strip() or "自动暂停"
    progress_rounds = max(0, total_rounds - remaining_rounds)
    resume_hint = _build_pause_resume_hint(rt)
    countdown_msg = (
        "⏸️ 暂停倒计时提醒（自动） ⏸️\n\n"
        f"📌 暂停原因：{reason}\n"
        "🧱 当前状态：暂停中，本局不会下注\n"
        f"🔢 倒计时：{remaining_rounds} 局\n"
        f"📊 暂停进度：{progress_rounds}/{total_rounds}\n"
        f"🔄 {resume_hint}\n"
        "ℹ️ 若恢复时仍不满足风控门槛，会再次自动暂停"
    )

    if hasattr(user_ctx, "pause_countdown_message") and user_ctx.pause_countdown_message:
        await cleanup_message(client, user_ctx.pause_countdown_message)
    user_ctx.pause_countdown_message = await send_to_admin(client, countdown_msg, user_ctx, global_config)
    rt["pause_countdown_last_remaining"] = remaining_rounds


async def _trigger_deep_risk_pause_after_settle(
    client,
    user_ctx: UserContext,
    global_config: dict,
    risk_pause: dict,
    next_sequence: int,
    settled_count: int,
) -> bool:
    """在结算阶段触发深度风控暂停（连输里程碑），命中后立即通知。"""
    rt = user_ctx.state.runtime
    if not bool(rt.get("risk_deep_enabled", True)):
        return False
    if not risk_pause.get("deep_trigger", False):
        return False

    deep_milestone = int(risk_pause.get("deep_milestone", 0))
    deep_cap = int(risk_pause.get("deep_level_cap", 3))
    if deep_milestone <= 0 or deep_cap <= 0:
        return False

    # 长龙盘面放宽：避免“连续长龙 + 深度风控”叠加导致长时间停摆。
    original_deep_cap = deep_cap
    tail_len, tail_side = _get_history_tail_streak(user_ctx.state.history)
    deep_cap_adjust_reason = ""
    if tail_len >= RISK_DEEP_LONG_DRAGON_TAIL_LEN:
        deep_cap = max(1, min(deep_cap, int(RISK_DEEP_LONG_DRAGON_MAX_PAUSE_ROUNDS)))
        if deep_cap < original_deep_cap:
            side_text = "大" if tail_side == 1 else "小"
            deep_cap_adjust_reason = (
                f"盘面尾部{tail_len}连{side_text}，本层暂停上限由 {original_deep_cap} 调整为 {deep_cap}"
            )

    level_label = f"深度风控（{deep_milestone}连输档）"
    model_eval = {
        **risk_pause,
        "level": f"DEEP_{deep_milestone}",
        "level_label": level_label,
    }
    model_pause_rounds, model_reason, model_source = await _suggest_pause_rounds_by_model(
        user_ctx,
        model_eval,
        max_pause=deep_cap,
    )
    initial_amount = int(rt.get("initial_amount", 500) or 500)
    min_pause_rounds = 1
    if deep_milestone >= 6:
        min_pause_rounds = 2
    if initial_amount >= 10000 and deep_milestone >= 3:
        min_pause_rounds = max(min_pause_rounds, 2)
    if initial_amount >= 20000 and deep_milestone >= 6:
        min_pause_rounds = max(min_pause_rounds, 3)
    pause_rounds = max(min_pause_rounds, min(deep_cap, int(model_pause_rounds)))
    _enter_pause(rt, pause_rounds, f"深度风控暂停（{deep_milestone}连输档）")
    rt["risk_pause_snapshot_count"] = settled_count
    rt["risk_pause_block_hits"] = int(rt.get("risk_pause_block_hits", 0)) + 1
    rt["risk_pause_block_rounds"] = int(rt.get("risk_pause_block_rounds", 0)) + pause_rounds

    deep_triggered = _get_deep_triggered_milestones(rt)
    if deep_milestone not in deep_triggered:
        deep_triggered.append(deep_milestone)
    rt["risk_deep_triggered_milestones"] = sorted(set(int(x) for x in deep_triggered))

    wins = risk_pause.get("wins", 0)
    total = risk_pause.get("total", 0)
    win_rate = risk_pause.get("win_rate", 0.0) * 100
    reason_text = "、".join(risk_pause.get("reasons", [])) or f"连输达到{deep_milestone}档位"
    if deep_cap_adjust_reason:
        reason_text = f"{reason_text}；{deep_cap_adjust_reason}"
    resume_hint = _build_pause_resume_hint(rt)
    pause_msg = (
        f"⛔ 自动风控暂停 ⛔\n\n"
        f"触发层级：{level_label}\n"
        f"触发原因：{reason_text}\n"
        f"最近{total}笔胜率：{wins}/{total}（{win_rate:.1f}%）\n"
        f"触发点：第 {next_sequence} 手下注前\n"
        f"模型建议：{model_pause_rounds} 局（来源：{model_source}）\n"
        f"本次暂停：{pause_rounds} 局（该层上限 {deep_cap}，最低保护 {min_pause_rounds} 局，不占基础预算）\n"
        f"模型依据：{model_reason}\n"
        f"暂停期间：保留当前倍投进度，不会重置首注\n"
        f"{resume_hint}"
    )

    if hasattr(user_ctx, "risk_pause_message") and user_ctx.risk_pause_message:
        await cleanup_message(client, user_ctx.risk_pause_message)
    user_ctx.risk_pause_message = await send_to_admin(client, pause_msg, user_ctx, global_config)
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=pause_rounds,
    )
    rt["risk_pause_priority_notified"] = True
    user_ctx.save_state()

    log_event(
        logging.INFO,
        "settle",
        "结算阶段触发深度风控暂停",
        user_id=user_ctx.user_id,
        data=(
            f"milestone={deep_milestone}, next_seq={next_sequence}, "
            f"pause_rounds={pause_rounds}, source={model_source}"
        ),
    )
    return True


async def _handle_goal_pause_after_settle(
    client,
    user_ctx: UserContext,
    global_config: dict,
) -> bool:
    """
    统一处理“炸号/盈利达成”触发的暂停。
    仅做结构收敛，不改变原有阈值与重置语义。
    """
    state = user_ctx.state
    rt = state.runtime

    explode_count = int(rt.get("explode_count", 0))
    explode = int(rt.get("explode", 5))
    period_profit = int(rt.get("period_profit", 0))
    profit_target = int(rt.get("profit", 1000000))

    if not (explode_count >= explode or period_profit >= profit_target):
        return False

    if not rt.get("flag", True):
        return False
    rt["flag"] = False

    notify_type = "explode" if explode_count >= explode else "profit"
    log_event(logging.INFO, 'settle', '触发通知', user_id=user_ctx.user_id, data=f'type={notify_type}')

    if notify_type == "profit":
        date_str = datetime.now().strftime("%m月%d日")
        current_round_str = f"{datetime.now().strftime('%Y%m%d')}_{rt.get('current_round', 1)}"
        round_bet_count = sum(
            1 for entry in state.bet_sequence_log
            if str(entry.get("bet_id", "")).startswith(current_round_str)
        )
        win_msg = _build_success_ops_card(
            "✅ 本轮盈利达成",
            outcome="本轮已达到盈利条件，系统会按设定进入暂停观察。",
            fields=[
                ("轮次", f"{date_str} 第 {rt.get('current_round', 1)} 轮"),
                ("收益", f"{period_profit / 10000:.2f} 万"),
                ("共下注", f"{round_bet_count} 次"),
            ],
            action="建议查看 `status`，确认暂停局数和下一轮状态。",
        )
        await send_message_v2(client, "win", win_msg, user_ctx, global_config)
    else:
        explode_msg = _build_alert_ops_card(
            "⚠️ 炸号保护已触发",
            impact="当前轮次触发炸号保护，系统会立即暂停观察。",
            fields=[
                ("当前轮次", f"第 {rt.get('current_round', 1)} 轮"),
                ("本轮收益", f"{period_profit / 10000:.2f} 万"),
            ],
            action="先看 `status`，确认暂停局数与当前资金状态。",
        )
        await send_message_v2(client, "explode", explode_msg, user_ctx, global_config)

    configured_stop_rounds = int(rt.get("stop", 3) if notify_type == "explode" else rt.get("profit_stop", 5))
    pause_reason = "炸号保护暂停" if notify_type == "explode" else "盈利达成暂停"
    _enter_pause(rt, configured_stop_rounds, pause_reason)
    rt["bet_sequence_count"] = 0

    if period_profit >= profit_target:
        rt["current_round"] = int(rt.get("current_round", 1)) + 1
        rt["current_bet_seq"] = 1

    rt["explode_count"] = 0
    rt["period_profit"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0
    rt["bet_amount"] = int(rt.get("initial_amount", 500))
    _clear_lose_recovery_tracking(rt)

    resume_hint = _build_pause_resume_hint(rt)
    account_balance_raw = max(0, int(rt.get("account_balance", 0) or 0))
    if str(rt.get("balance_status", "") or "").strip() in {"auth_failed", "network_error", "unknown"} and account_balance_raw <= 0:
        account_balance_text = _format_account_balance_text(rt)
    else:
        account_balance_text = f"{account_balance_raw / 10000:.2f} 万"
    gambling_fund_text = f"{max(0, int(rt.get('gambling_fund', 0) or 0)) / 10000:.2f} 万"
    pause_msg = _build_alert_ops_card(
        f"⛔ {'炸号保护暂停' if notify_type == 'explode' else '盈利达成暂停'}",
        impact="系统已进入目标暂停，当前策略状态会被保留，不会重置首注。",
        fields=[
            ("原因", "被炸保护" if notify_type == 'explode' else "盈利达成"),
            ("本次暂停", f"{configured_stop_rounds} 局"),
            ("恢复提示", resume_hint),
            ("账户资金", account_balance_text),
            ("菠菜资金", gambling_fund_text),
        ],
        action="等待倒计时结束；如需复核可执行 `status`。",
    )
    log_event(
        logging.INFO,
        'settle',
        '暂停押注',
        user_id=user_ctx.user_id,
        data=f'type={notify_type}, stop_count={configured_stop_rounds}'
    )
    await send_message_v2(
        client,
        "goal_pause",
        pause_msg,
        user_ctx,
        global_config,
        title=f"菠菜机器人 {user_ctx.config.name} {'炸号' if notify_type == 'explode' else '盈利'}暂停",
        desp=pause_msg,
    )
    await _refresh_pause_countdown_notice(
        client,
        user_ctx,
        global_config,
        remaining_rounds=configured_stop_rounds,
    )
    return True


def count_consecutive(history):
    """统计连续出现次数 - 与master版本一致"""
    result_counts = {"大": {}, "小": {}}
    if not history:
        return result_counts
    
    current_streak = 1
    for i in range(1, len(history)):
        if history[i] == history[i-1]:
            current_streak += 1
        else:
            key = "大" if history[i-1] == 1 else "小"
            result_counts[key][current_streak] = result_counts[key].get(current_streak, 0) + 1
            current_streak = 1
    
    key = "大" if history[-1] == 1 else "小"
    result_counts[key][current_streak] = result_counts[key].get(current_streak, 0) + 1
    
    return result_counts


def count_lose_streaks(bet_sequence_log):
    """统计连输次数 - 与master版本一致"""
    lose_streaks = {}
    current_streak = 0
    
    for entry in bet_sequence_log:
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if result not in {"赢", "输"}:
            continue
        profit = int(entry.get("profit", 0) or 0)
        if result == "输" and profit < 0:
            current_streak += 1
        else:
            if current_streak > 0:
                lose_streaks[current_streak] = lose_streaks.get(current_streak, 0) + 1
            current_streak = 0
    
    if current_streak > 0:
        lose_streaks[current_streak] = lose_streaks.get(current_streak, 0) + 1
    
    return lose_streaks


def _get_resolved_account_bet_logs(state: UserState) -> List[Dict[str, Any]]:
    logs = state.bet_sequence_log if isinstance(getattr(state, "bet_sequence_log", None), list) else []
    resolved: List[Dict[str, Any]] = []
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("result") not in {"赢", "输"}:
            continue
        resolved.append(entry)
    return resolved


def _build_stats_report(state: UserState, windows: Optional[List[int]] = None) -> str:
    windows = windows or [1000, 500, 200, 100]
    history = state.history if isinstance(state.history, list) else []
    resolved_logs = _get_resolved_account_bet_logs(state)

    def _build_section(title: str, categories: List[str], source_length: int, source_getter) -> List[str]:
        labels: List[int] = []
        section_stats = {category: [] for category in categories}
        all_ns = set()

        for window in windows:
            label = int(window)
            if label <= 0 or label in labels:
                continue
            labels.append(label)
            snapshot = source_getter(label) if source_length >= label else {}
            for category in categories:
                bucket = snapshot.get(category, {}) if isinstance(snapshot, dict) else {}
                section_stats[category].append(bucket)
                all_ns.update(bucket.keys())

        if not labels:
            return [title, "\u6682\u65e0\u6570\u636e", ""]

        label_width = max(3, max(len(str(label)) for label in labels))
        header = "\u7c7b\u522b |" + "".join(f" {str(label).rjust(label_width)} |" for label in labels)
        divider = "-" * len(header)
        lines = [title, "=" * len(header), header, divider]

        for category in categories:
            lines.append(category)
            if not all_ns:
                row = " --  |" + "".join(f" {'-'.center(label_width)} |" for _ in labels)
                lines.append(row)
                lines.append("")
                continue
            for n in sorted(all_ns, reverse=True):
                row = f" {str(n).center(2)}  |"
                for i in range(len(labels)):
                    count = section_stats[category][i].get(n, 0)
                    value = str(count) if count > 0 else "-"
                    row += f" {value.center(label_width)} |"
                lines.append(row)
            lines.append("")
        return lines

    market_lines = _build_section(
        "\u76d8\u53e3\u7edf\u8ba1\uff08\u8fde\u5927 / \u8fde\u5c0f\uff09",
        ["\u8fde\u5927", "\u8fde\u5c0f"],
        len(history),
        lambda actual: {
            "\u8fde\u5927": count_consecutive(history[-actual:]).get("\u5927", {}),
            "\u8fde\u5c0f": count_consecutive(history[-actual:]).get("\u5c0f", {}),
        },
    )
    bet_lines = _build_section(
        "\u62bc\u6ce8\u7edf\u8ba1\uff08\u8fde\u8f93\uff09",
        ["\u8fde\u8f93"],
        len(resolved_logs),
        lambda actual: {"\u8fde\u8f93": count_lose_streaks(resolved_logs[-actual:])},
    )

    lines = [
        "\u6700\u8fd1\u5c40\u6570\u201c\u8fde\u5927\u3001\u8fde\u5c0f\u3001\u8fde\u8f93\u201d\u7edf\u8ba1",
        "",
        "\u8bf4\u660e\uff1a\u76d8\u53e3\u7edf\u8ba1\u57fa\u4e8e history\uff1b\u62bc\u6ce8\u7edf\u8ba1\u57fa\u4e8e\u5f53\u524d\u8d26\u53f7\u5168\u90e8\u5df2\u7ed3\u7b97\u62bc\u6ce8\u8bb0\u5f55\u3002",
        "",
        *market_lines,
        *bet_lines,
    ]
    pre_block = escape_html("\n".join(lines).rstrip())
    return f"\U0001f4ca \u7edf\u8ba1\u6982\u89c8\n\n<pre>{pre_block}</pre>"


def _clear_lose_recovery_tracking(rt: dict) -> None:
    """清理连输回补跟踪状态，避免跨轮次残留导致误发“连输已终止”消息。"""
    rt["lose_notify_pending"] = False
    rt["lose_start_info"] = {}


def _is_valid_lose_range(start_round, start_seq, end_round, end_seq) -> bool:
    """校验连输区间是否有效（起点不晚于终点）。"""
    try:
        sr = int(start_round)
        ss = int(start_seq)
        er = int(end_round)
        es = int(end_seq)
    except Exception:
        return False

    if sr > er:
        return False
    if sr == er and ss > es:
        return False
    return True


def generate_bet_id(user_ctx: UserContext) -> str:
    """生成押注 ID（与 master 逻辑一致：按天重置轮次）。"""
    rt = user_ctx.state.runtime
    current_date = datetime.now().strftime("%Y%m%d")
    if current_date != rt.get("last_reset_date", ""):
        rt["current_round"] = 1
        rt["current_bet_seq"] = 1
        rt["last_reset_date"] = current_date
    return f"{current_date}_{rt.get('current_round', 1)}_{rt.get('current_bet_seq', 1)}"


def format_bet_id(bet_id):
    """将押注 ID 转换为直观格式，如 '3月14日第 1 轮第 12 次'。"""
    try:
        date_str, round_num, seq_num = str(bet_id).split('_')
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        return f"{month}月{day}日第 {round_num} 轮第 {seq_num} 次"
    except Exception:
        return str(bet_id)


def get_settle_position(state, rt):
    """
    获取当前结算对应的轮次与序号。
    优先用当前结算 bet_id，回退到 current_bet_seq - 1。
    """
    settle_round = int(rt.get("current_round", 1))
    settle_seq = max(1, int(rt.get("current_bet_seq", 1)) - 1)
    if state.bet_sequence_log:
        last_bet_id = str(state.bet_sequence_log[-1].get("bet_id", ""))
        import re
        match = re.match(r"^\d{8}_(\d+)_(\d+)$", last_bet_id)
        if match:
            settle_round = int(match.group(1))
            settle_seq = int(match.group(2))
    return settle_round, settle_seq


def _format_recent_binary(history: list, window: int) -> str:
    """
    格式化最近 N 局结果为二进制字符串
    与 master 版本 _format_recent_binary 一致
    """
    if len(history) < window:
        window = len(history)
    if window <= 0:
        return ""
    recent = history[-window:]
    return "".join(str(x) for x in recent)


def _get_current_streak(history: list):
    """返回当前连串长度与方向（与 master 一致）。"""
    if not history:
        return 0, "大"
    tail = history[-1]
    streak = 1
    for value in reversed(history[:-1]):
        if value == tail:
            streak += 1
        else:
            break
    return streak, ("大" if tail == 1 else "小")


def _compact_reason_text(reason: str, max_len: int = 96) -> str:
    """压缩风控原因，避免在通知里输出超长分析（与 master 一致）。"""
    if not reason:
        return "策略风控触发"
    first_line = str(reason).splitlines()[0].strip()
    return first_line if len(first_line) <= max_len else first_line[: max_len - 1] + "…"


def generate_mobile_bet_report(
    history: list,
    direction: str,
    amount: int,
    sequence_count: int,
    bet_id: str = ""
) -> str:
    streak_len, streak_side = _get_current_streak(history)
    bet_label = format_bet_id(bet_id) if bet_id else "本次"
    return _build_ops_card(
        f"🎯 **{bet_label}押注执行** 🎯",
        summary="本局下注指令已发送，等待结算结果回写。",
        fields=[
            ("😀 连续押注", f"{sequence_count} 次"),
            ("⚡ 押注方向", direction),
            ("💵 押注本金", _format_money_message(amount)),
            (f"📊 当前连{streak_side}", streak_len),
        ],
        action="本局无需额外操作，建议等待结果通知。",
    )


def _build_fund_pause_message(current_fund: int) -> str:
    return _build_alert_ops_card(
        "⛔ 资金不足，已暂停押注",
        impact="当前资金无法覆盖下一手下注，系统已自动暂停以避免继续扩大风险。",
        fields=[
            ("当前剩余", f"{max(0, int(current_fund or 0)) / 10000:.2f} 万"),
            ("恢复方式", "`gf [金额]`"),
        ],
        action="补充资金后执行 `gf [金额]`，再用 `status` 确认恢复情况。",
    )


def _build_version_catalog_message(result: Dict[str, Any]) -> str:
    current = result.get("current", {})
    current_short = current.get("short_commit", "unknown") or "unknown"
    current_tag_exact = current.get("current_tag", "") or ""
    nearest_tag = current.get("nearest_tag", "") or ""
    if current_tag_exact:
        current_tag_display = current_tag_exact.upper()
    elif nearest_tag:
        current_tag_display = f"无（最近: {nearest_tag}）"
    else:
        current_tag_display = "无"

    remote_head = result.get("remote_head", {}) or {}
    remote_head_short = remote_head.get("short_commit", "-") or "-"
    remote_head_tag = result.get("remote_head_tag", "") or ""
    pending_tags = result.get("pending_tags", [])
    recent_tags = result.get("recent_tags", []) or []
    recent_commits = result.get("recent_commits", []) or []

    latest_tag_target = pending_tags[0] if pending_tags else ""
    if latest_tag_target:
        latest_tag_line = f"{latest_tag_target}（可执行 `update {latest_tag_target}`）"
    else:
        latest_tag_line = "无（已是最新）"

    latest_commit_target = ""
    if remote_head_short not in {"", "-", "unknown"} and remote_head_short != current_short:
        latest_commit_target = remote_head_short

    if latest_commit_target:
        extra_tag_note = f" | Tag:{remote_head_tag}" if remote_head_tag else " | 未打Tag"
        latest_commit_line = f"{latest_commit_target}{extra_tag_note}（可执行 `update {latest_commit_target}`）"
    else:
        latest_commit_line = "无（已是最新）"

    highlights = []
    if recent_tags:
        highlights.append("最近版本：")
        for idx, item in enumerate(recent_tags[:3], 1):
            tag = item.get("tag", "") or "-"
            date = item.get("date", "") or "-"
            summary = item.get("summary", "") or "-"
            highlights.append(f"{idx}. {tag} | {date} | {summary}")
    if recent_commits:
        highlights.append("")
        highlights.append("最近提交：")
        for idx, item in enumerate(recent_commits[:3], 1):
            short_commit = item.get("short_commit", "") or "-"
            date = item.get("date", "") or "-"
            summary = item.get("summary", "") or "-"
            suffix = "（当前）" if short_commit == current_short else ""
            highlights.append(f"{idx}. {short_commit} | {date} | {summary}{suffix}")

    return _build_ops_card(
        "📦 版本信息概览",
        summary="当前版本状态与可更新目标如下。",
        fields=[
            ("当前 Tag", current_tag_display),
            ("当前 Commit", current_short),
            ("最新 Tag", latest_tag_line),
            ("最新 Commit", latest_commit_line),
        ],
        action="需要升级可执行 `update <版本或提交>`；完成后记得执行 `restart`。",
        note="\n".join(highlights).strip(),
    )


async def _process_settle_slim(client, event, user_ctx: UserContext, global_config: dict):
    state = user_ctx.state
    rt = state.runtime
    text = event.message.message

    try:
        match = re.search(r"已结算[^0-9]*(?:结果[为中])?[^0-9]*(\d+)\s*(大|小)", text)
        if not match:
            return

        settle_msg_id = int(getattr(event, "id", 0) or 0)
        last_settle_msg_id = int(rt.get("last_settle_message_id", 0) or 0)
        if settle_msg_id > 0 and settle_msg_id == last_settle_msg_id:
            return
        if settle_msg_id > 0:
            rt["last_settle_message_id"] = settle_msg_id

        result_type = match.group(2)
        is_big = result_type == "大"
        result = 1 if is_big else 0

        if _verbose_runtime_diag_enabled():
            log_event(
                logging.INFO,
                'settle',
                '收到结算并开始回写',
                user_id=user_ctx.user_id,
                category='runtime',
                **_build_runtime_chain_diag(
                    rt,
                    state,
                    settle_msg_id=settle_msg_id,
                    settle_result=result_type,
                ),
            )

        try:
            rt["account_balance"] = await fetch_balance(user_ctx)
            rt["balance_status"] = "success"
        except Exception as e:
            log_event(logging.WARNING, 'settle', '鑾峰彇璐︽埛浣欓澶辫触锛屼娇鐢ㄩ粯璁ゅ€?', user_id=user_ctx.user_id, data=str(e))
            rt["balance_status"] = "network_error"

        state.history.append(result)
        state.history = state.history[-2000:]
        await _push_market_broadcast_snapshot(user_ctx, state.history)
        lose_end_payload = None

        async def _apply_settle_fund_safety_guard() -> None:
            next_bet_amount = calculate_bet_amount(rt)
            if next_bet_amount <= 0:
                rt["fund_pause_notified"] = False
                return
            if not is_fund_available(user_ctx, next_bet_amount):
                if not rt.get("fund_pause_notified", False):
                    display_fund = max(0, rt.get("gambling_fund", 0))
                    mes = _build_fund_pause_message(display_fund)
                    await send_message_v2(
                        client,
                        "fund_pause",
                        mes,
                        user_ctx,
                        global_config,
                        title=f"菠菜机器人 {user_ctx.config.name} 资金暂停",
                        desp=mes,
                    )
                    rt["fund_pause_notified"] = True
                rt["bet"] = False
                rt["bet_on"] = False
                rt["mode_stop"] = True
            else:
                rt["fund_pause_notified"] = False

        if rt.get("bet", False):
            settled_entry = _get_latest_open_bet_entry(state)
            if settled_entry is None:
                log_event(
                    logging.WARNING,
                    'settle',
                    'runtime 标记待结算但未找到 open bet',
                    user_id=user_ctx.user_id,
                    category='warning',
                    **_build_runtime_chain_diag(
                        rt,
                        state,
                        settle_msg_id=settle_msg_id,
                        settle_result=result_type,
                    ),
                )
                rt["bet"] = False
                user_ctx.save_state()
                return

            prediction = int(rt.get("bet_type", -1))
            win = (is_big and prediction == 1) or (not is_big and prediction == 0)
            bet_amount = int(rt.get("bet_amount", 500))
            profit = int(bet_amount * 0.99) if win else -bet_amount
            settle_round, settle_seq = get_settle_position(state, rt)
            old_lose_count = int(rt.get("lose_count", 0))
            direction = "大" if prediction == 1 else "小"
            result_text = "赢" if win else "输"

            log_event(
                logging.INFO,
                'settle',
                '结算前链路诊断',
                user_id=user_ctx.user_id,
                category='runtime',
                **_build_runtime_chain_diag(
                    rt,
                    state,
                    settle_msg_id=settle_msg_id,
                    settled_bet_id=str(settled_entry.get("bet_id", "unknown")),
                    settled_amount=bet_amount,
                    settled_prediction=direction,
                    old_lose_count=old_lose_count,
                ),
            )

            rt["bet"] = False
            state.bet_type_history.append(prediction)
            rt["gambling_fund"] = rt.get("gambling_fund", 0) + profit
            rt["earnings"] = rt.get("earnings", 0) + profit
            rt["period_profit"] = rt.get("period_profit", 0) + profit
            rt["win_total"] = rt.get("win_total", 0) + (1 if win else 0)
            rt["win_count"] = rt.get("win_count", 0) + 1 if win else 0
            rt["lose_count"] = rt.get("lose_count", 0) + 1 if not win else 0
            rt["status"] = 1 if win else 0

            settled_entry["result"] = result_text
            settled_entry["profit"] = profit
            settled_entry["settled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            active_chain_summary = _summarize_effective_bet_chain(state)
            recent_resolved_summary = _summarize_recent_resolved_chain(state)
            if not win:
                rt["bet_sequence_count"] = max(
                    int(active_chain_summary.get("continuous_count", 0)),
                    int(old_lose_count) + 1,
                )
                rt["lose_count"] = max(
                    int(active_chain_summary.get("lose_count", 0)),
                    int(old_lose_count) + 1,
                )
                rt["bet_amount"] = int(active_chain_summary.get("last_amount", bet_amount) or bet_amount)

            if _verbose_runtime_diag_enabled():
                log_event(
                    logging.INFO,
                    'settle',
                    '结算后链路回写完成',
                    user_id=user_ctx.user_id,
                    category='business',
                    **_build_runtime_chain_diag(
                        rt,
                        state,
                        settle_msg_id=settle_msg_id,
                        settled_bet_id=str(settled_entry.get("bet_id", "unknown")),
                        settle_outcome=result_text,
                        settle_profit=profit,
                        chain_sequence_after=int(rt.get("bet_sequence_count", 0) or 0),
                        chain_lose_after=int(rt.get("lose_count", 0) or 0),
                        next_bet_amount=int(calculate_bet_amount(rt) or 0),
                    ),
                )

            if not win:
                if rt.get("lose_count", 0) == 1:
                    _clear_lose_recovery_tracking(rt)
                    rt["lose_start_info"] = {
                        "round": settle_round,
                        "seq": settle_seq,
                        "fund": rt.get("gambling_fund", 0) + bet_amount
                    }
                warning_lose_count = rt.get("warning_lose_count", 3)
                if rt.get("lose_count", 0) >= warning_lose_count:
                    rt["lose_notify_pending"] = True
                    total_losses = int(active_chain_summary.get("total_losses", abs(profit)))
                    warn_msg = _build_alert_ops_card(
                        f"⚠️ {int(rt.get('lose_count', 0))} 连输告警",
                        impact="当前链路已进入高关注状态，请重点关注下一手与账户余额变化。",
                        fields=[
                            ("🔢 时间", f"{datetime.now().strftime('%m月%d日')} 第 {settle_round} 轮第 {settle_seq} 次"),
                            ("📋 预设名称", rt.get('current_preset_name', 'none')),
                            ("😀 连续押注", f"{int(active_chain_summary.get('continuous_count', rt.get('bet_sequence_count', 0)))} 次"),
                            ("⚡ 押注方向", direction),
                            ("💵 押注本金", _format_money_message(bet_amount)),
                            ("💰 累计损失", _format_money_message(total_losses)),
                            ("💰 账户余额", f"{rt.get('account_balance', 0) / 10000:.2f} 万"),
                            ("💰 菠菜余额", f"{rt.get('gambling_fund', 0) / 10000:.2f} 万"),
                        ],
                        action="建议立即查看 `status`；如不准备继续，可直接执行 `pause`。",
                    )
                    if hasattr(user_ctx, "lose_streak_message") and user_ctx.lose_streak_message:
                        await cleanup_message(client, user_ctx.lose_streak_message)
                    user_ctx.lose_streak_message = await send_message_v2(
                        client,
                        "lose_streak",
                        warn_msg,
                        user_ctx,
                        global_config,
                        title=f"菠菜机器人 {user_ctx.config.name} 连输告警",
                        desp=warn_msg
                    )

            if win and rt.get("lose_notify_pending", False):
                warning_lose_count = int(rt.get("warning_lose_count", 3))
                lose_start_info = rt.get("lose_start_info", {})
                start_round = lose_start_info.get("round", "?")
                start_seq = lose_start_info.get("seq", "?")
                end_round = settle_round
                end_seq = settle_seq
                total_loss = int(recent_resolved_summary.get("total_losses", 0))
                resolved_chain = recent_resolved_summary.get("chain", []) if isinstance(recent_resolved_summary.get("chain"), list) else []
                total_profit = sum(int(item.get("profit", 0) or 0) for item in resolved_chain if isinstance(item, dict))
                current_balance = int(rt.get("account_balance", 0) or 0)
                current_fund = int(rt.get("gambling_fund", 0) or 0)
                if int(old_lose_count) >= warning_lose_count and _is_valid_lose_range(start_round, start_seq, end_round, end_seq):
                    continuous_count = max(
                        int(recent_resolved_summary.get("continuous_count", 0)),
                        int(old_lose_count) + 1,
                    )
                    lose_end_payload = {
                        "start_round": start_round,
                        "start_seq": start_seq,
                        "end_round": end_round,
                        "end_seq": end_seq,
                        "lose_count": old_lose_count,
                        "continuous_count": continuous_count,
                        "total_loss": total_loss,
                        "total_profit": total_profit,
                        "account_balance": current_balance,
                        "gambling_fund": current_fund,
                    }
                _clear_lose_recovery_tracking(rt)
            elif win:
                _clear_lose_recovery_tracking(rt)

            user_ctx.save_state()

            result_amount = _format_money_message(int(bet_amount * 0.99) if win else bet_amount)
            last_bet_id = settled_entry.get("bet_id", "") if isinstance(settled_entry, dict) else ""
            bet_id = format_bet_id(last_bet_id) if last_bet_id else f"{datetime.now().strftime('%m月%d日')}第 {rt.get('current_round', 1)} 轮第 {rt.get('current_bet_seq', 1)} 次"
            settle_sequence_count = int(recent_resolved_summary.get("continuous_count", rt.get("bet_sequence_count", 0)))

            mes = _build_ops_card(
                f"🔢 {bet_id}押注结果 🔢",
                summary="本局已完成结算，状态和资金已同步更新。",
                fields=[
                    ("😀 连续押注", f"{settle_sequence_count} 次"),
                    ("⚡ 押注方向", direction),
                    ("💵 押注本金", _format_money_message(bet_amount)),
                    ("📉 输赢结果", f"{result_text} {result_amount}"),
                    ("🎲 开奖结果", result_type),
                    ("", rt.get('last_predict_info', 'N/A')),
                ],
                action="如需继续观察，等待下一次盘口；如需复核当前状态，请执行 `status`。",
            )
            await send_to_admin(client, mes, user_ctx, global_config)

            if win or rt.get("lose_count", 0) >= rt.get("lose_stop", 13):
                rt["bet_sequence_count"] = 0
                rt["bet_amount"] = int(rt.get("initial_amount", 500))

        await _apply_settle_fund_safety_guard()

        if len(state.history) % 5 == 0:
            user_ctx.save_state()

        await _handle_goal_pause_after_settle(client, user_ctx, global_config)

        if hasattr(user_ctx, 'dashboard_message') and user_ctx.dashboard_message:
            await cleanup_message(client, user_ctx.dashboard_message)
        await _refresh_dashboard_message_slim(client, user_ctx, global_config)

        current_total = int(rt.get("total", 0))
        last_stats_total = int(rt.get("stats_last_report_total", 0))
        if (
            len(state.history) > 5
            and current_total > 0
            and current_total % AUTO_STATS_INTERVAL_ROUNDS == 0
            and current_total != last_stats_total
        ):
            mes = _build_stats_report(state)
            stats_message = await send_message_v2(
                client,
                "info",
                mes,
                user_ctx,
                global_config,
                parse_mode="html",
            )
            user_ctx.stats_message = stats_message
            rt["stats_last_report_total"] = current_total
            if stats_message:
                asyncio.create_task(delete_later(client, stats_message.chat_id, stats_message.id, AUTO_STATS_DELETE_DELAY_SECONDS))

        if lose_end_payload:
            date_str = datetime.now().strftime("%m月%d日")
            start_round = lose_end_payload.get("start_round", "?")
            start_seq = lose_end_payload.get("start_seq", "?")
            end_round = lose_end_payload.get("end_round", "?")
            end_seq = lose_end_payload.get("end_seq", "?")
            lose_count = int(lose_end_payload.get("lose_count", 0))
            if str(start_round) == str(end_round):
                range_text = f"{date_str} 第 {start_round} 轮第 {start_seq} 次 至 第 {end_seq} 次"
            else:
                range_text = f"{date_str} 第 {start_round} 轮第 {start_seq} 次 至 第 {end_round} 轮第 {end_seq} 次"
            rec_msg = _build_success_ops_card(
                f"✅ {lose_count} 连输已终止！ ✅",
                outcome="本轮回补已经结束，系统已回写收益与当前余额。",
                fields=[
                    ("🔢 时间", range_text),
                    ("📋 预设名称", rt.get('current_preset_name', 'none')),
                    ("😀 连续押注", f"{lose_end_payload.get('continuous_count', lose_count)} 次"),
                    ("⚠️ 本局连输", f"{lose_count} 次"),
                    ("💰 本局盈利", _format_money_message(lose_end_payload.get('total_profit', 0))),
                    ("💰 账户余额", f"{lose_end_payload.get('account_balance', rt.get('account_balance', 0)) / 10000:.2f} 万"),
                    ("💰 菠菜资金剩余", f"{lose_end_payload.get('gambling_fund', rt.get('gambling_fund', 0)) / 10000:.2f} 万"),
                ],
                action="建议关注是否已回到首注，并继续观察下一次盘口。",
            )
            if hasattr(user_ctx, "lose_streak_message") and user_ctx.lose_streak_message:
                await cleanup_message(client, user_ctx.lose_streak_message)
                user_ctx.lose_streak_message = None
            await send_message_v2(client, "lose_end", rec_msg, user_ctx, global_config)
    except Exception as e:
        log_event(logging.ERROR, 'settle', '结算失败', user_id=user_ctx.user_id, data=str(e))
        await send_to_admin(
            client,
            _build_ops_card(
                "❌ 结算处理失败",
                summary="本次结算回写没有完成。",
                fields=[("错误", str(e)[:180])],
                action="建议稍后关注下一条结果；如持续异常，请执行 `status` 或 `restart`。",
            ),
            user_ctx,
            global_config,
        )


async def process_settle(client, event, user_ctx: UserContext, global_config: dict):
    return await _process_settle_slim(client, event, user_ctx, global_config)


async def delete_later(client, chat_id, message_id, delay=10):
    """延迟指定秒数后删除消息。"""
    await asyncio.sleep(delay)
    try:
        bot_token = str(getattr(client, "_admin_console_bot_token", "") or "").strip()
        bot_chat_id = getattr(client, "_admin_console_bot_chat_id", None)
        if bot_token and bot_chat_id is not None and str(chat_id) == str(bot_chat_id):
            url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
            await _post_json_async(url, {"chat_id": chat_id, "message_id": message_id}, timeout=5)
        else:
            await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def handle_model_command_multiuser(client, event, args, user_ctx: UserContext, global_config: dict):
    """处理 model 命令 - 与master版本handle_model_command一致"""
    rt = user_ctx.state.runtime
    sub_cmd = args[0] if args else "list"
    
    # 兼容 "model id list" 和 "model id XX"
    if sub_cmd == "id":
        if len(args) < 2:
            sub_cmd = "list"
        elif args[1] == "list":
            sub_cmd = "list"
        else:
            sub_cmd = "select"
            args = ["select", args[1]]

    if sub_cmd == "list":
        models = user_ctx.config.ai.get("models", {})
        entries = []
        idx = 1
        current_model_id = rt.get("current_model_id", "")
        
        for k, m in models.items():
            if m.get("enabled", True):
                current = "（当前）" if m.get('model_id') == current_model_id else ""
                entries.append(f"{idx}. `{m.get('model_id', 'unknown')}` {current}".strip())
                idx += 1
        await _send_command_ops_card(
            client,
            event,
            user_ctx,
            global_config,
            "🤖 可用模型列表",
            summary="以下是当前账号可用的模型。",
            fields=[("模型", "\n".join(entries) if entries else "暂无可用模型")],
            action="切换模型可执行 `model select <编号或ID>`。",
        )
        
    elif sub_cmd in ["select", "use", "switch"]:
        if len(args) < 2:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ 缺少模型目标",
                summary="当前没有提供要切换的模型编号或 ID。",
                action="请执行 `model select 1` 或 `model select qwen3-coder-plus`。",
            )
            return
            
        target_id = args[1]
        models = user_ctx.config.ai.get("models", {})
        
        # 支持数字编号选择
        if target_id.isdigit():
            idx = int(target_id)
            enabled_models = [m for m in models.values() if m.get("enabled", True)]
            if 1 <= idx <= len(enabled_models):
                target_id = enabled_models[idx-1].get('model_id', '')
            else:
                await _send_command_ops_card(
                    client,
                    event,
                    user_ctx,
                    global_config,
                    "❌ 模型编号无效",
                    summary=f"编号 {idx} 不在当前可选范围内。",
                    action="请先执行 `model list` 查看可用编号。",
                )
                return
        
        # 验证模型是否存在
        model_exists = any(m.get('model_id') == target_id for m in models.values() if m.get("enabled"))
        if not model_exists:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ 模型不可用",
                summary=f"模型 `{target_id}` 不存在或当前未启用。",
                action="请先执行 `model list` 确认可用模型。",
            )
            return
            
        await _send_command_ops_card(
            client,
            event,
            user_ctx,
            global_config,
            "🔄 正在切换模型",
            summary="系统正在切换默认模型。",
            fields=[("目标模型", f"`{target_id}`")],
            action="请等待切换结果返回。",
        )
        
        # 切换模型
        rt["current_model_id"] = target_id
        user_ctx.save_state()
        
        await _send_command_ops_card(
            client,
            event,
            user_ctx,
            global_config,
            "✅ 模型切换成功",
            summary="后续新局会使用这个模型继续判断。",
            fields=[
                ("当前模型", f"`{target_id}`"),
                ("连接状态", "正常"),
            ],
            action="建议等待下一局生效，或执行 `status` 查看当前概览。",
        )
        log_event(logging.INFO, 'model', '切换模型', user_id=user_ctx.user_id, model=target_id)
            
    else:
        await _send_command_ops_card(
            client,
            event,
            user_ctx,
            global_config,
            "❓ 未知模型命令",
            summary="当前子命令无法识别。",
            fields=[("用法", "`model list`\n`model select <id>`")],
            action="建议先执行 `model list` 查看当前可用模型。",
        )


async def handle_apikey_command_multiuser(client, event, args, user_ctx: UserContext, global_config: dict):
    """处理 apikey 命令：show/set/add/del。"""
    rt = user_ctx.state.runtime
    sub_cmd = (args[0].lower() if args else "show")
    ai_cfg = user_ctx.config.ai if isinstance(user_ctx.config.ai, dict) else {}
    keys = _normalize_ai_keys(ai_cfg)

    if sub_cmd in ("show", "list", "ls"):
        if not keys:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "🔐 当前未配置 AI key",
                summary="当前账号还没有可用的模型密钥。",
                action="请执行 `apikey set <新key>`。",
            )
            return
        lines = []
        for idx, key in enumerate(keys, 1):
            lines.append(f"{idx}. `{_mask_api_key(key)}`")
        await _send_command_ops_card(
            client,
            event,
            user_ctx,
            global_config,
            "🔐 当前账号 AI key 列表",
            summary="已按脱敏方式展示，避免在聊天窗口泄露完整 key。",
            fields=[("Key", "\n".join(lines))],
            action="可执行 `apikey set` / `apikey add` / `apikey del`。",
        )
        return

    if sub_cmd in ("set", "add"):
        if len(args) < 2:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ 缺少 key 参数",
                summary="当前没有提供新的 key。",
                action=f"请执行 `apikey {sub_cmd} <新key>`。",
            )
            return

        new_key = str(args[1]).strip()
        if not new_key:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ key 不能为空",
                summary="当前输入的 key 为空。",
                action=f"请重新执行 `apikey {sub_cmd} <新key>`。",
            )
            return

        if sub_cmd == "set":
            updated_keys = [new_key]
        else:
            updated_keys = list(keys)
            if new_key in updated_keys:
                await _send_command_ops_card(
                    client,
                    event,
                    user_ctx,
                    global_config,
                    "⚠️ 无需重复添加",
                    summary="该 key 已经存在于当前账号配置中。",
                    action="如需覆盖全部 key，请使用 `apikey set <新key>`。",
                )
                return
            updated_keys.append(new_key)

        new_ai = dict(ai_cfg)
        new_ai["api_keys"] = updated_keys
        new_ai.pop("api_key", None)
        try:
            config_path = user_ctx.update_ai_config(new_ai)
            _clear_ai_key_issue(rt)
            user_ctx.save_state()
            model_mgr = user_ctx.get_model_manager()
            model_mgr.load_models()
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "✅ AI key 已更新",
                summary="新的 key 已写入配置并重新加载。",
                fields=[
                    ("文件", f"`{os.path.basename(config_path)}`"),
                    ("当前 key 数量", len(updated_keys)),
                ],
                action="如需核对当前状态，建议执行 `apikey show`。",
            )
        except Exception as e:
            log_event(logging.ERROR, 'apikey', '写入 key 失败', user_id=user_ctx.user_id, error=str(e))
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ AI key 更新失败",
                summary="本次写入配置没有完成。",
                fields=[("错误", str(e)[:160])],
                action="建议检查配置文件权限后再重试。",
            )
        return

    if sub_cmd in ("del", "rm", "remove"):
        if len(args) < 2:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ 缺少删除序号",
                summary="当前没有提供要删除的 key 序号。",
                action="请执行 `apikey del <序号>`。",
            )
            return
        try:
            idx = int(str(args[1]).strip())
        except ValueError:
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ 序号格式错误",
                summary="删除序号必须是整数。",
                action="请执行 `apikey del <序号>`。",
            )
            return

        if idx < 1 or idx > len(keys):
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ 序号超出范围",
                summary=f"当前 key 数量只有 {len(keys)} 个。",
                action="请先执行 `apikey show` 查看当前序号。",
            )
            return

        updated_keys = list(keys)
        updated_keys.pop(idx - 1)
        new_ai = dict(ai_cfg)
        new_ai["api_keys"] = updated_keys
        new_ai.pop("api_key", None)
        try:
            config_path = user_ctx.update_ai_config(new_ai)
            if not updated_keys:
                _mark_ai_key_issue(rt, "管理员删除了全部 key")
            user_ctx.save_state()
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "✅ AI key 已删除",
                summary=f"第 {idx} 个 key 已从当前账号配置中移除。",
                fields=[
                    ("文件", f"`{os.path.basename(config_path)}`"),
                    ("剩余 key 数量", len(updated_keys)),
                ],
                action="如需确认当前可用 key，请执行 `apikey show`。",
            )
        except Exception as e:
            log_event(logging.ERROR, 'apikey', '删除 key 失败', user_id=user_ctx.user_id, error=str(e))
            await _send_command_ops_card(
                client,
                event,
                user_ctx,
                global_config,
                "❌ AI key 删除失败",
                summary="本次删除没有完成。",
                fields=[("错误", str(e)[:160])],
                action="建议检查配置文件权限后再重试。",
            )
        return

    await _send_command_ops_card(
        client,
        event,
        user_ctx,
        global_config,
        "❓ 未知 key 命令",
        summary="当前子命令无法识别。",
        fields=[("用法", "`apikey show`\n`apikey set <key>`\n`apikey add <key>`\n`apikey del <序号>`")],
        action="建议先执行 `apikey show` 查看当前状态。",
    )


async def process_user_command(client, event, user_ctx: UserContext, global_config: dict):
    """处理用户命令。"""
    state = user_ctx.state
    rt = state.runtime
    presets = user_ctx.presets
    
    text = event.raw_text.strip()
    if not text:
        return

    my = text.split()
    if not my:
        return

    raw_cmd = str(my[0]).strip()
    if not raw_cmd:
        return

    # 仅解析“命令形态”文本，避免把通知正文(⚠️/🔢/📊开头)当成未知命令。
    # 兼容 `/help` 与中文命令别名 `暂停/恢复`。
    normalized_cmd = raw_cmd[1:] if raw_cmd.startswith("/") else raw_cmd
    if not normalized_cmd:
        return

    allowed_cn_cmds = {"暂停", "恢复"}
    is_ascii_cmd = (
        normalized_cmd[0].isalpha()
        and all(ch.isalnum() or ch in {"_", "-"} for ch in normalized_cmd)
    )
    if normalized_cmd not in allowed_cn_cmds and not is_ascii_cmd:
        return

    cmd = normalized_cmd.lower()
    
    safe_log_text = text[:50]
    if cmd == "apikey":
        safe_log_text = f"{raw_cmd} ***"
    masked_text, was_masked = _mask_command_text(text)
    append_interaction_event(
        user_ctx,
        direction="inbound",
        kind="command",
        channel="admin_chat",
        text=masked_text,
        command=cmd,
        masked=was_masked,
        chat_id=getattr(event, "chat_id", None),
        message_id=getattr(event, "id", None),
    )
    log_event(logging.INFO, 'user_cmd', '处理用户命令', user_id=user_ctx.user_id, data=safe_log_text)
    
    try:
        # ========== help命令 ==========
        if cmd == "help":
            mes = _build_help_card()
            log_event(logging.INFO, 'user_cmd', '显示帮助', user_id=user_ctx.user_id)
            message = await send_message_v2(
                client,
                "info",
                mes,
                user_ctx,
                global_config,
                parse_mode="html",
            )
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        if cmd == "xx":
            target_groups = []
            target_groups.extend(_iter_targets(user_ctx.config.groups.get("zq_group", [])))

            # 去重并保持顺序
            unique_groups = []
            seen = set()
            for gid in target_groups:
                key = str(gid)
                if key in seen:
                    continue
                seen.add(key)
                unique_groups.append(gid)

            if not unique_groups:
                message = await send_to_admin(
                    client,
                    _build_ops_card(
                        "⚠️ 未配置可清理群组",
                        summary="当前账号没有配置 `zq_group`，因此无法执行群消息清理。",
                        action="如需使用 `xx`，请先在账号配置里补充 `zq_group`。",
                    ),
                    user_ctx,
                    global_config,
                )
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
                return

            deleted_total = 0
            failed_groups = []
            scanned_groups = 0

            for gid in unique_groups:
                try:
                    msg_ids = [msg.id async for msg in client.iter_messages(gid, from_user="me", limit=500)]
                    scanned_groups += 1
                    if msg_ids:
                        await client.delete_messages(gid, msg_ids)
                        deleted_total += len(msg_ids)
                except Exception as e:
                    failed_groups.append(f"{gid}: {str(e)[:40]}")

            mes = (
                _build_ops_card(
                    "🧹 群组消息已清理",
                    summary="已按当前配置扫描并清理我发送的历史消息。",
                    fields=[
                        ("扫描群组", scanned_groups),
                        ("删除消息", deleted_total),
                    ],
                    action="如需再次清理，可重新执行 `xx`。",
                    note="\n".join(failed_groups[:5]) if failed_groups else "",
                )
            )

            log_event(
                logging.INFO,
                'user_cmd',
                '执行xx清理',
                user_id=user_ctx.user_id,
                groups=scanned_groups,
                deleted=deleted_total,
                failed=len(failed_groups),
            )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return

        # pause/resume - 暂停/恢复押注
        if cmd in ("pause", "暂停"):
            if rt.get("manual_pause", False):
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "⏸️ 当前账号已是暂停状态",
                        summary="系统已处于手动暂停，无需重复执行。",
                        action="如需继续，请执行 `resume`；如需查看详情，请执行 `status`。",
                    ),
                    user_ctx,
                    global_config,
                )
                return
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["switch"] = True
            rt["bet_on"] = False
            rt["bet"] = False
            rt["mode_stop"] = True
            rt["manual_pause"] = True
            _clear_lose_recovery_tracking(rt)
            user_ctx.save_state()
            mes = _build_ops_card(
                "⏸️ 已暂停当前账号押注",
                summary="当前账号后续不会自动下注，已有状态会被保留。",
                action="如需恢复，请执行 `resume`；如需查看当前链路，请执行 `status`。",
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '暂停押注', user_id=user_ctx.user_id)
            return
        
        if cmd in ("resume", "恢复"):
            await _clear_pause_countdown_notice(client, user_ctx)
            rt["switch"] = True
            rt["bet_on"] = True
            rt["bet"] = False
            rt["mode_stop"] = True
            rt["manual_pause"] = False
            user_ctx.save_state()
            mes = _build_ops_card(
                "▶️ 已恢复当前账号押注",
                summary="后续会继续等待有效盘口触发，不会立即补发历史下注。",
                action="建议执行 `status` 确认当前状态，并等待下一次盘口。",
            )
            await send_to_admin(client, mes, user_ctx, global_config)
            log_event(logging.INFO, 'user_cmd', '恢复押注', user_id=user_ctx.user_id)
            return

        # risk - 基础/深度风控开关
        # st - 启动预设 - 与master一致
        if cmd == "st" and len(my) > 1:
            preset_name = my[1]
            if preset_name in presets:
                preset = presets[preset_name]
                rt["continuous"] = int(preset[0])
                rt["lose_stop"] = int(preset[1])
                rt["lose_once"] = float(preset[2])
                rt["lose_twice"] = float(preset[3])
                rt["lose_three"] = float(preset[4])
                rt["lose_four"] = float(preset[5])
                rt["initial_amount"] = int(preset[6])
                rt["current_preset_name"] = preset_name
                rt["bet_amount"] = int(preset[6])
                await _clear_pause_countdown_notice(client, user_ctx)
                rt["switch"] = True
                rt["manual_pause"] = False
                rt["bet_on"] = True
                rt["mode_stop"] = True
                rt["bet"] = False  # st 命令不直接设置 bet=True，等待真实盘口触发下注
                rt["risk_deep_triggered_milestones"] = []
                rt["fund_pause_notified"] = False
                rt["limit_stop_notified"] = False
                _clear_lose_recovery_tracking(rt)
                user_ctx.save_state()
                
                mes = _build_ops_card(
                    f"🎯 预设启动成功: {preset_name}",
                    summary="当前账号已经切换到新的预设，后续将按这套参数进入可下注状态。",
                    fields=[
                        ("策略参数", f"{preset[0]} {preset[1]} {preset[2]} {preset[3]} {preset[4]} {preset[5]} {preset[6]}"),
                    ],
                    action="建议留意本轮自动测算结果，并用 `status` 确认当前状态。",
                )
                log_event(logging.INFO, 'user_cmd', '启动预设', user_id=user_ctx.user_id, preset=preset_name)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
                await yc_command_handler_multiuser(
                    client,
                    event,
                    [preset_name],
                    user_ctx,
                    global_config,
                    auto_trigger=True,
                )
            else:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 预设不存在",
                        summary=f"当前账号没有找到名为 `{preset_name}` 的预设。",
                        action="请先执行 `yss` 查看可用预设，或用 `ys` 新建一个预设。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        
        # stats - 查看连大、连小、连输统计
        if cmd == "stats":
            if len(state.history) < 10:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "📉 暂无法生成统计",
                        summary="当前历史数据不足 10 局，统计结果参考意义不够。",
                        action="建议再观察几局后执行 `stats`。",
                    ),
                    user_ctx,
                    global_config,
                )
                return

            mes = _build_stats_report(state)
            
            log_event(logging.INFO, 'user_cmd', '查看统计', user_id=user_ctx.user_id)
            message = await send_message_v2(
                client,
                "info",
                mes,
                user_ctx,
                global_config,
                parse_mode="html",
            )
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
            return
        
        # status - 查看仪表盘 - 与master一致
        if cmd == "status":
            dashboard = format_dashboard(user_ctx)
            message = await send_message_v2(
                client,
                "dashboard",
                dashboard,
                user_ctx,
                global_config,
                parse_mode="html",
            )
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # ========== 参数设置命令 ==========
        # gf - 设置资金 - 与master一致
        if cmd == "gf":
            old_fund = rt.get("gambling_fund", 0)
            if len(my) == 1:
                rt["gambling_fund"] = rt.get("gambling_fund", 2000000)
                mes = _build_ops_card(
                    "✅ 菠菜资金已重置",
                    summary="当前账号的菠菜资金已恢复为默认值。",
                    fields=[("当前金额", _format_money_message(rt['gambling_fund']))],
                    action="建议执行 `status` 确认资金与状态是否符合预期。",
                )
            elif len(my) == 2:
                try:
                    new_fund = int(my[1])
                    if new_fund < 0:
                        mes = _build_ops_card(
                            "❌ 菠菜资金设置失败",
                            summary="菠菜资金不能设置为负数。",
                            action="请执行 `gf [金额]`，金额必须是大于等于 0 的整数。",
                        )
                    else:
                        account_balance = rt.get("account_balance", 0)
                        if new_fund > account_balance:
                            new_fund = account_balance
                            mes = _build_ops_card(
                                "⚠️ 菠菜资金已自动调整",
                                summary="输入金额超过当前账户余额，系统已自动压到可用上限。",
                                fields=[("当前金额", _format_money_message(new_fund))],
                                action="建议执行 `balance` 或 `status` 再确认余额状态。",
                            )
                        else:
                            mes = _build_ops_card(
                                "✅ 菠菜资金已更新",
                                summary="新的菠菜资金已经写入当前账号状态。",
                                fields=[("当前金额", _format_money_message(new_fund))],
                                action="建议执行 `status` 确认后续下一手金额是否符合预期。",
                            )
                        rt["gambling_fund"] = new_fund
                except ValueError:
                    mes = _build_ops_card(
                        "❌ 菠菜资金设置失败",
                        summary="金额格式无效，必须是整数。",
                        action="请执行 `gf [金额]`，例如 `gf 1000000`。",
                    )
            else:
                mes = _build_ops_card(
                    "❌ 菠菜资金命令格式错误",
                    summary="当前命令参数数量不正确。",
                    action="正确用法：`gf` 或 `gf [金额]`。",
                )
            
            log_event(logging.INFO, 'user_cmd', '设置资金', user_id=user_ctx.user_id, mes=mes)
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            
            if rt.get("gambling_fund", 0) != old_fund:
                log_event(logging.INFO, 'user_cmd', '资金变更', user_id=user_ctx.user_id, 
                         old=old_fund, new=rt.get("gambling_fund", 0))
                await check_bet_status(client, user_ctx, global_config)
            return
        
        if cmd == "stf":
            if len(my) == 2:
                try:
                    target_wan = float(my[1])
                    if target_wan <= 0:
                        raise ValueError
                    rt["profit"] = int(target_wan * 10000)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 本轮目标金额已更新",
                        fields=[("当前目标", f"{rt['profit'] / 10000:.2f} 万")],
                    )
                    log_event(logging.INFO, 'user_cmd', '设置本轮目标金额', user_id=user_ctx.user_id, profit=rt["profit"])
                    message = await send_to_admin(client, mes, user_ctx, global_config)
                    asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                    if message:
                        asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
                except ValueError:
                    await send_to_admin(
                        client,
                        _build_ops_card(
                            "❌ 目标金额设置失败",
                            summary="请输入大于 0 的数字。",
                            action="正确用法：`stf [数字]`，例如 `stf 100`。",
                        ),
                        user_ctx,
                        global_config,
                    )
                return
            await send_to_admin(
                client,
                _build_ops_card(
                    "❌ 目标金额设置失败",
                    summary="当前参数数量不正确。",
                    action="正确用法：`stf [数字]`，例如 `stf 100`。",
                ),
                user_ctx,
                global_config,
            )
            return

        # wlc - 设置连输告警阈值 - 与master一致
        if cmd == "wlc":
            if len(my) > 1:
                try:
                    warning_count = int(my[1])
                    if warning_count < 1:
                        raise ValueError
                    rt["warning_lose_count"] = warning_count
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 连输告警阈值已更新",
                        summary="后续达到该连输次数时，系统会发出高优提醒。",
                        fields=[("当前阈值", f"{warning_count} 次")],
                        action="建议结合 `status` 观察当前链路压力。",
                    )
                    log_event(logging.INFO, 'user_cmd', '设置连输告警阈值', user_id=user_ctx.user_id, warning_lose_count=warning_count)
                except ValueError:
                    mes = _build_ops_card(
                        "❌ 告警阈值设置失败",
                        summary="阈值必须是大于等于 1 的整数。",
                        action="请执行 `wlc <次数>`。",
                    )
            else:
                mes = _build_ops_card(
                    "📌 当前连输告警阈值",
                    summary="这是当前账号触发连输告警的阈值。",
                    fields=[("当前阈值", f"{rt.get('warning_lose_count', 3)} 次")],
                    action="如需调整，请执行 `wlc <次数>`。",
                )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return

        if cmd == "mfb":
            current_enabled = bool(rt.get("stat_fallback_bet_enabled", _is_stat_fallback_bet_enabled(user_ctx)))
            if len(my) == 1:
                state_text = "开启" if current_enabled else "关闭"
                action_text = (
                    "模型链不可用时，改用统计兜底继续下注"
                    if current_enabled
                    else "模型链不可用时不再统计兜底下注，等待模型恢复后继续"
                )
                mes = _build_ops_card(
                    "📌 模型兜底开关",
                    fields=[
                        ("当前状态", state_text),
                        ("异常时动作", action_text),
                    ],
                )
            elif len(my) == 2 and my[1].lower() in {"on", "off"}:
                enabled = my[1].lower() == "on"
                try:
                    rt["stat_fallback_bet_enabled"] = enabled
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        f"✅ 模型兜底开关已{'开启' if enabled else '关闭'}",
                        summary=(
                            "当前策略：模型链不可用时，改用统计兜底继续下注"
                            if enabled
                            else "当前策略：模型链不可用时不再统计兜底下注，等待模型恢复后继续"
                        ),
                    )
                    log_event(
                        logging.INFO,
                        'user_cmd',
                        '设置模型兜底开关',
                        user_id=user_ctx.user_id,
                        enabled=enabled,
                    )
                except Exception as e:
                    mes = _build_ops_card(
                        "❌ 模型兜底开关设置失败",
                        summary=f"配置写入失败：{str(e)[:80]}",
                        action="正确用法：`mfb [on/off]`，例如 `mfb off`。",
                    )
            else:
                mes = _build_ops_card(
                    "❌ 模型兜底开关设置失败",
                    summary="当前参数数量或格式不正确。",
                    action="正确用法：`mfb [on/off]`，例如 `mfb off`。",
                )
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 20))
            return
        
        # model - 模型管理 - 使用与master一致的handle_model_command
        if cmd == "model":
            await handle_model_command_multiuser(client, event, my[1:], user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd == "apikey":
            await handle_apikey_command_multiuser(client, event, my[1:], user_ctx, global_config)
            # 防止 key 在命令消息中长期可见
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            return

        # ========== 发布更新命令 ==========
        if cmd in ("ver", "version"):
            result = await asyncio.to_thread(list_version_catalog, None, 3)
            if not result.get("success"):
                mes = _build_ops_card(
                    "❌ 版本查询失败",
                    summary="当前无法读取版本信息。",
                    fields=[("错误", result.get('error', 'unknown'))],
                    action="建议稍后重试；若持续失败，可先检查仓库状态或网络。",
                )
            else:
                mes = _build_version_catalog_message(result)

            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return

        if cmd in ("update", "up", "upnow", "upref", "upcommit"):
            target_ref = my[1].strip() if len(my) > 1 else ""
            await send_to_admin(
                client,
                _build_release_ops_card(
                    "🔄 开始更新",
                    summary="系统已开始拉取并切换版本。",
                    target_version=target_ref or "latest",
                ),
                user_ctx,
                global_config,
            )
            result = await asyncio.to_thread(update_to_version, None, target_ref)
            if result.get("success"):
                if result.get("no_change"):
                    await send_to_admin(
                        client,
                        _build_release_ops_card(
                            "✅ 无需更新",
                            summary=result.get('message', '当前已是目标版本。'),
                            restart_required=False,
                        ),
                        user_ctx,
                        global_config,
                    )
                else:
                    after = result.get("after", {})
                    resolved = result.get("resolved_target", "") or result.get("target_ref", target_ref or "latest")
                    mes = _build_release_ops_card(
                        "✅ 更新成功",
                        summary="代码已切到目标版本。",
                        target_version=resolved,
                        current_version=after.get('display_version', after.get('short_commit', 'unknown')),
                        restart_required=True,
                        restart_command="`restart`",
                    )
                    await send_to_admin(client, mes, user_ctx, global_config)
            else:
                blocking_paths = result.get("blocking_paths", [])
                detail = result.get("detail", "")
                blocking_text = " / ".join(blocking_paths[:5]) if blocking_paths else ""
                await send_to_admin(
                    client,
                    _build_release_ops_card(
                        "❌ 更新失败",
                        summary="本次更新没有完成。",
                        target_version=target_ref or "latest",
                        error=result.get('error', 'unknown'),
                        blocking_files=blocking_text,
                        note=detail[:200] if detail else "",
                    ),
                    user_ctx,
                    global_config,
                )
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("reback", "rollback", "uprollback"):
            target_ref = my[1].strip() if len(my) > 1 else ""
            if not target_ref:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 缺少回退目标",
                        summary="当前没有提供要回退到的版本、提交或分支。",
                        action="请执行 `reback <版本号|commit|branch>`。",
                    ),
                    user_ctx,
                    global_config,
                )
                return

            await send_to_admin(
                client,
                _build_release_ops_card(
                    "↩️ 开始回退",
                    summary="系统已开始切换到指定历史版本。",
                    target_version=target_ref,
                ),
                user_ctx,
                global_config,
            )
            result = await asyncio.to_thread(reback_to_version, None, target_ref)
            if result.get("success"):
                after = result.get("after", {})
                resolved = result.get("resolved_target", target_ref)
                mes = _build_release_ops_card(
                    "✅ 回退成功",
                    summary="代码已回退到目标版本。",
                    target_version=resolved,
                    current_version=after.get('display_version', after.get('short_commit', 'unknown')),
                    restart_required=True,
                    restart_command="`restart`",
                )
                await send_to_admin(client, mes, user_ctx, global_config)
            else:
                mes = _build_release_ops_card(
                    "❌ 回退失败",
                    summary="本次回退没有完成。",
                    target_version=target_ref,
                    error=result.get('error', 'unknown'),
                    note=str(result.get('detail'))[:200] if result.get("detail") else "",
                )
                await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return

        if cmd in ("restart", "reboot"):
            service_name = resolve_systemd_service_name()
            if service_name:
                mes = _build_release_ops_card(
                    "♻️ 开始重启",
                    summary="系统会在 2 秒后通过 systemd 重启服务。",
                    extra_fields=[("服务名", service_name)],
                )
            else:
                mes = _build_release_ops_card(
                    "♻️ 开始重启",
                    summary="系统会在 2 秒后自动重启当前进程。",
                    extra_fields=[("重启方式", "当前进程")],
                )
            await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 3))
            asyncio.create_task(restart_process())
            return
        
        # ========== 数据管理命令 ==========
        # res - 重置命令 - 与master一致
        if cmd == "res":
            if len(my) > 1:
                if my[1] == "tj":
                    # 重置统计
                    rt["win_total"] = 0
                    rt["total"] = 0
                    rt["stats_last_report_total"] = 0
                    rt["earnings"] = 0
                    rt["period_profit"] = 0
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 统计数据已重置",
                        summary="收益、胜率和计数类统计已经清空。",
                        action="建议执行 `status` 查看当前状态是否符合预期。",
                    )
                    log_event(logging.INFO, 'user_cmd', '重置统计数据', user_id=user_ctx.user_id, action='completed')
                elif my[1] == "state":
                    # 重置状态
                    state.history = []
                    state.bet_type_history = []
                    rt["win_total"] = 0
                    rt["total"] = 0
                    rt["stats_last_report_total"] = 0
                    rt["earnings"] = 0
                    rt["period_profit"] = 0
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 状态文件已重置",
                        summary="历史、统计和运行态已清空到初始状态。",
                        action="如需重新开始，建议先执行 `st <预设名>`。",
                    )
                    log_event(logging.INFO, 'user_cmd', '重置状态文件', user_id=user_ctx.user_id, action='completed')
                elif my[1] == "bet":
                    # 重置押注策略
                    rt["win_count"] = 0
                    rt["lose_count"] = 0
                    rt["bet_sequence_count"] = 0
                    rt["bet_reset_log_index"] = len(state.bet_sequence_log)
                    rt["explode_count"] = 0
                    rt["bet_amount"] = int(rt.get("initial_amount", 500))
                    rt["bet"] = False
                    rt["bet_on"] = False
                    rt["stop_count"] = 0
                    rt["flag"] = True
                    rt["mode_stop"] = True
                    rt["manual_pause"] = False
                    rt["pause_count"] = 0
                    rt["current_bet_seq"] = 1
                    rt["risk_pause_acc_rounds"] = 0
                    rt["risk_pause_snapshot_count"] = -1
                    rt["risk_pause_cycle_active"] = False
                    rt["risk_pause_recovery_passes"] = 0
                    rt["risk_base_hit_streak"] = 0
                    rt["risk_pause_level1_hit"] = False
                    rt["risk_deep_triggered_milestones"] = []
                    _clear_lose_recovery_tracking(rt)
                    user_ctx.save_state()
                    mes = _build_ops_card(
                        "✅ 押注策略已重置",
                        summary="当前连押链路已清空，后续会按首注重新开始。",
                        fields=[("初始金额", rt.get('initial_amount', 500))],
                        action="建议执行 `status` 确认当前状态，再等待下一次盘口。",
                    )
                    log_event(logging.INFO, 'user_cmd', '重置押注策略', user_id=user_ctx.user_id, action='completed')
                else:
                    mes = _build_ops_card(
                        "❌ 重置命令无效",
                        summary="当前重置类型无法识别。",
                        action="可用命令：`res tj`、`res state`、`res bet`。",
                    )
                    log_event(logging.WARNING, 'user_cmd', '无效重置命令', user_id=user_ctx.user_id, cmd=text)
            else:
                mes = _build_ops_card(
                    "📌 请选择重置类型",
                    summary="当前没有指定具体要重置的内容。",
                    action="请执行 `res tj`、`res state` 或 `res bet`。",
                )
            
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            return
        
        # explain - 查看最近一次模型判断依据
        if cmd == "explain":
            last_logic_audit = rt.get("last_logic_audit", "")
            if last_logic_audit:
                log_event(logging.INFO, 'user_cmd', '查看决策解释', user_id=user_ctx.user_id)
                mes = f"🧠 **最近一次模型判断依据**\n```json\n{last_logic_audit}\n```"
            else:
                mes = "当前还没有可展示的模型判断记录，请等下一次有效判断后再查看。"
            message = await send_to_admin(client, mes, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return

        # balance - 查询余额 - 与master一致
        if cmd == "balance":
            try:
                balance = await fetch_balance(user_ctx)
                rt["account_balance"] = balance
                user_ctx.save_state()
                mes = _build_ops_card(
                    "💰 账户余额查询成功",
                    summary="余额已刷新到当前最新值。",
                    fields=[
                        ("账户余额", _format_money_message(balance)),
                        ("菠菜资金", _format_money_message(rt.get("gambling_fund", 0))),
                    ],
                    action="如需继续操作，建议再执行 `status` 查看完整概览。",
                )
                await send_to_admin(client, mes, user_ctx, global_config)
                log_event(logging.INFO, 'user_cmd', '查询余额', user_id=user_ctx.user_id, balance=balance)
            except Exception as e:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 账户余额查询失败",
                        summary="本次没有成功获取最新余额。",
                        fields=[("错误", str(e)[:180])],
                        action="建议稍后重试；若持续失败，请检查 Cookie 或网络状态。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        
        # ========== 预设管理命令 ==========
        # ys - 保存预设 - 与master一致
        if cmd == "ys" and len(my) >= 9:
            try:
                preset_name = my[1]
                ys = [int(my[2]), int(my[3]), float(my[4]), float(my[5]), float(my[6]), float(my[7]), int(my[8])]
                presets[preset_name] = ys
                user_ctx.save_presets()
                rt["current_preset_name"] = preset_name
                user_ctx.save_state()
                mes = _build_ops_card(
                    f"✅ 预设保存成功: {preset_name}",
                    summary="新的预设参数已经写入当前账号，并设置为当前预设。",
                    fields=[("策略参数", f"{ys[0]} {ys[1]} {ys[2]} {ys[3]} {ys[4]} {ys[5]} {ys[6]}")],
                    action=f"建议执行 `st {preset_name}` 或 `status` 确认当前状态。",
                )
                log_event(logging.INFO, 'user_cmd', '保存预设策略', user_id=user_ctx.user_id, preset=preset_name, params=ys)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            except (ValueError, IndexError) as e:
                await send_to_admin(
                    client,
                    _build_ops_card(
                        "❌ 预设保存失败",
                        summary="参数格式不正确，当前预设没有写入。",
                        fields=[("错误", str(e)[:180])],
                        action="请按 `ys [名] ...` 的格式重新输入完整参数。",
                    ),
                    user_ctx,
                    global_config,
                )
            return
        if cmd == "ys":
            await send_to_admin(
                client,
                _build_ops_card(
                    "❌ 预设保存失败",
                    summary="当前参数数量不足，预设没有写入。",
                    action="请按 `ys [名] [连续] [停] [倍1] [倍2] [倍3] [倍4] [首注]` 重新输入。",
                ),
                user_ctx,
                global_config,
            )
            return
        
        # yss - 查看/删除预设 - 与master一致
        if cmd == "yss":
            if len(my) > 2 and my[1] == "dl":
                # 删除预设
                preset_name = my[2]
                if preset_name in presets:
                    del presets[preset_name]
                    user_ctx.save_presets()
                    mes = _build_ops_card(
                        f"✅ 预设删除成功: {preset_name}",
                        summary="该预设已经从当前账号配置中移除。",
                        action="建议执行 `yss` 再确认剩余预设。",
                    )
                    log_event(logging.INFO, 'user_cmd', '删除预设', user_id=user_ctx.user_id, preset=preset_name)
                else:
                    mes = _build_ops_card(
                        "❌ 预设删除失败",
                        summary="目标预设不存在或命令格式不正确。",
                        action="请先执行 `yss` 查看当前预设名称，再执行 `yss dl [名]`。",
                    )
                    log_event(logging.WARNING, 'user_cmd', '删除预设失败', user_id=user_ctx.user_id, cmd=text)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 10))
            else:
                # 查看所有预设
                if len(presets) > 0:
                    max_key_length = max(len(str(k)) for k in presets.keys())
                    preset_lines = "\n".join(f"'{k.ljust(max_key_length)}': {v}" for k, v in presets.items())
                    mes = f"📚 当前预设列表\n\n{preset_lines}"
                    log_event(logging.INFO, 'user_cmd', '查看预设', user_id=user_ctx.user_id)
                else:
                    mes = _build_ops_card(
                        "📚 当前暂无预设",
                        summary="当前账号还没有保存任何自定义预设。",
                        action="可执行 `ys [名] ...` 新建预设，或直接使用内置预设。",
                    )
                    log_event(logging.INFO, 'user_cmd', '暂无预设', user_id=user_ctx.user_id)
                message = await send_to_admin(client, mes, user_ctx, global_config)
                asyncio.create_task(delete_later(client, event.chat_id, event.id, 60))
                if message:
                    asyncio.create_task(delete_later(client, message.chat_id, message.id, 60))
            return
        
        # ========== 测算命令 ==========
        if cmd == "yc":
            # 测算命令 - 与master一致
            await yc_command_handler_multiuser(client, event, my[1:], user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            return
        
        # ========== 多用户管理命令 ==========
        # users - 查看所有用户
        if cmd == "users":
            # 获取当前用户信息
            user_info = _build_ops_card(
                "👤 当前用户信息",
                summary="以下是当前账号的核心运行信息。",
                fields=[
                    ("账号", f"{user_ctx.config.name} (ID: {user_ctx.user_id})"),
                    ("菠菜资金", _format_money_message(rt.get('gambling_fund', 0))),
                    ("状态", get_bet_status_text(rt)),
                    ("预设", rt.get('current_preset_name', '无')),
                    ("模型", rt.get('current_model_id', 'default')),
                    ("胜率", f"{rt.get('win_total', 0)}/{rt.get('total', 0)}"),
                ],
                action="如果需要更完整的运行态，请执行 `status`。",
            )
            message = await send_to_admin(client, user_info, user_ctx, global_config)
            asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
            if message:
                asyncio.create_task(delete_later(client, message.chat_id, message.id, 30))
            return
        
        # 未知命令
        log_event(logging.DEBUG, 'user_cmd', '未知命令', user_id=user_ctx.user_id, data=text[:50])
        message = await send_to_admin(
            client,
            _build_ops_card(
                "❓ 未知命令",
                summary=f"`{cmd}` 不是当前支持的命令。",
                action="请执行 `help` 查看可用命令列表。",
            ),
            user_ctx,
            global_config,
        )
        asyncio.create_task(delete_later(client, event.chat_id, event.id, 10))
        
    except Exception as e:
        log_event(logging.ERROR, 'user_cmd', '命令执行出错', user_id=user_ctx.user_id, error=str(e))
        await send_to_admin(
            client,
            _build_error_ops_card(
                "❌ 命令执行出错",
                problem="本次命令没有执行完成。",
                fields=[("错误", str(e)[:180])],
                action="建议稍后重试；若持续失败，可执行 `status` 确认当前状态。",
            ),
            user_ctx,
            global_config,
        )


async def check_bet_status(client, user_ctx: UserContext, global_config: dict):
    """检查押注状态 - 与master版本一致"""
    rt = user_ctx.state.runtime
    if rt.get("manual_pause", False):
        return
    next_bet_amount = calculate_bet_amount(rt)
    if next_bet_amount <= 0:
        rt["bet"] = False
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        if not rt.get("limit_stop_notified", False):
            lose_stop = int(rt.get("lose_stop", 13))
            await send_to_admin(
                client,
                _build_alert_ops_card(
                    "⚠️ 已达到预设连投上限",
                    impact="当前链路已经到达设定的最大连投次数，系统将保持暂停。",
                    fields=[("当前上限", f"{lose_stop} 手")],
                    action="如需继续，可切换预设，或执行 `res bet` 后重新启动。",
                ),
                user_ctx,
                global_config,
            )
            rt["limit_stop_notified"] = True
        user_ctx.save_state()
        return

    rt["limit_stop_notified"] = False
    if is_fund_available(user_ctx, next_bet_amount) and not rt.get("bet", False) and rt.get("switch", True) and rt.get("stop_count", 0) == 0:
        await _clear_pause_countdown_notice(client, user_ctx)
        # 这里只恢复“可下注状态”，不应提前标记为“已下注”。
        # bet=True 只能在真实点击下注成功后设置，避免结算时序误判。
        rt["bet"] = False
        rt["bet_on"] = True
        rt["mode_stop"] = True
        rt["pause_count"] = 0
        rt["fund_pause_notified"] = False
        user_ctx.save_state()
        mes = (
            "✅ 资金条件已满足，恢复可下注状态\n"
            f"当前资金：{rt.get('gambling_fund', 0) / 10000:.2f} 万\n"
            f"接续倍投金额：{_format_money_message(next_bet_amount)}\n"
            "说明：本提示仅表示“可下注”，实际下注仍以盘口事件触发为准"
        )
        await _send_transient_admin_notice(
            client,
            user_ctx,
            global_config,
            mes,
            ttl_seconds=120,
            attr_name="status_transition_message",
        )
    elif not is_fund_available(user_ctx, next_bet_amount):
        rt["bet_on"] = False
        rt["mode_stop"] = True
        _clear_lose_recovery_tracking(rt)
        if not rt.get("fund_pause_notified", False):
            mes = "⚠️ 菠菜资金不足，已自动暂停押注"
            await send_message_v2(
                client,
                "fund_pause",
                mes,
                user_ctx,
                global_config,
                title=f"菠菜机器人 {user_ctx.config.name} 资金风控暂停",
                desp=mes,
            )
            rt["fund_pause_notified"] = True
        user_ctx.save_state()


def _parse_yc_params(args, presets):
    if not args:
        return None, None, (
            "📊 **测算功能**\n\n"
            "用法:\n"
            "`yc [预设名]` - 测算已有预设\n"
            "`yc [参数...]` - 自定义参数测算\n\n"
            "例: `yc 5k` 或 `yc 1 12 3.0 2.5 2.2 2.1 5000`"
        )

    if args[0] in presets:
        preset = presets[args[0]]
        params = {
            "continuous": int(preset[0]),
            "lose_stop": int(preset[1]),
            "lose_once": float(preset[2]),
            "lose_twice": float(preset[3]),
            "lose_three": float(preset[4]),
            "lose_four": float(preset[5]),
            "initial_amount": int(preset[6]),
        }
        return params, args[0], None

    if len(args) >= 7:
        try:
            params = {
                "continuous": int(args[0]),
                "lose_stop": int(args[1]),
                "lose_once": float(args[2]),
                "lose_twice": float(args[3]),
                "lose_three": float(args[4]),
                "lose_four": float(args[5]),
                "initial_amount": int(args[6]),
            }
            return params, "自定义", None
        except ValueError:
            return None, None, "❌ 参数格式错误，请确保所有参数都是数字"

    return None, None, f"❌ 预设 `{args[0]}` 不存在，且参数不足7个"


def _calculate_yc_sequence(params):
    initial = max(0, int(params["initial_amount"]))
    lose_stop = max(1, int(params["lose_stop"]))
    table_steps = 15
    multipliers = [
        float(params["lose_once"]),
        float(params["lose_twice"]),
        float(params["lose_three"]),
        float(params["lose_four"]),
    ]
    max_single_bet_limit = 50_000_000
    start_streak = max(1, int(params["continuous"]))

    rows = []
    prev_bet = initial
    cumulative_loss = 0

    for i in range(table_steps):
        if i == 0:
            multiplier = 1.0
            bet = initial
        else:
            multiplier = multipliers[min(i - 1, 3)]
            bet = int(prev_bet * multiplier)

        if bet > max_single_bet_limit:
            bet = max_single_bet_limit

        cumulative_loss += bet
        profit_if_win = bet - (cumulative_loss - bet)
        rows.append(
            {
                "streak": start_streak + i,
                "multiplier": multiplier,
                "bet": bet,
                "profit_if_win": profit_if_win,
                "cumulative_loss": cumulative_loss,
            }
        )
        prev_bet = bet

    total_investment = rows[-1]["cumulative_loss"] if rows else 0
    max_bet = max((row["bet"] for row in rows), default=0)
    effective_rows = rows[:lose_stop]
    effective_streak = effective_rows[-1]["streak"] if effective_rows else start_streak
    effective_investment = effective_rows[-1]["cumulative_loss"] if effective_rows else 0
    effective_profit = effective_rows[-1]["profit_if_win"] if effective_rows else 0
    return {
        "rows": rows,
        "total_investment": total_investment,
        "max_bet": max_bet,
        "max_single_bet_limit": max_single_bet_limit,
        "start_streak": start_streak,
        "lose_stop": lose_stop,
        "table_steps": table_steps,
        "effective_rows": effective_rows,
        "effective_streak": effective_streak,
        "effective_investment": effective_investment,
        "effective_profit": effective_profit,
    }


def _build_yc_result_message(params, preset_name: str, current_fund: int, auto_trigger: bool) -> str:
    calc = _calculate_yc_sequence(params)
    rows = calc["rows"]
    effective_rows = calc["effective_rows"]
    effective_streak = calc["effective_streak"]
    effective_investment = calc["effective_investment"]
    effective_profit = calc["effective_profit"]
    max_single_bet_limit = calc["max_single_bet_limit"]

    def fmt_wan(value: int) -> str:
        return f"{value / 10000:,.1f}"

    def fmt_table_wan(value: int) -> str:
        wan = value / 10000
        if abs(wan) >= 1000:
            return f"{wan:,.0f}"
        return f"{wan:.1f}"

    header_line = "🔮 已根据当前预设自动测算\n" if auto_trigger else ""
    command_text = (
        f"{params['continuous']} {params['lose_stop']} "
        f"{params['lose_once']} {params['lose_twice']} {params['lose_three']} {params['lose_four']} {params['initial_amount']}"
    )

    fund_text = f"{fmt_wan(current_fund)}万" if current_fund > 0 else "未设置"
    cover_streak = 0
    cover_required = 0
    cover_profit = 0
    if current_fund > 0 and effective_rows:
        cover_rows = [row for row in effective_rows if row["cumulative_loss"] <= current_fund]
        if cover_rows:
            cover_row = cover_rows[-1]
            cover_streak = int(cover_row["streak"])
            cover_required = int(cover_row["cumulative_loss"])
            cover_profit = int(cover_row["profit_if_win"])
    elif effective_rows:
        cover_streak = int(effective_streak)
        cover_required = int(effective_investment)
        cover_profit = int(effective_profit)

    lines = []
    if header_line:
        lines.append(header_line.rstrip("\n"))
    lines.append("```")
    lines.extend(
        [
            "🎯 策略参数",
            f"预设名称：{preset_name}",
            f"菠菜资金：{fund_text}",
            f"策略命令: {command_text}",
            f"🏁 起始连数: {params['continuous']}",
            f"🔢 下注次数: {params['lose_stop']}次",
            f"💰 首注金额: {fmt_wan(int(params['initial_amount']))}万",
            f"💰 单注上限: {max_single_bet_limit / 10000:,.0f}万",
            "",
            "🎯 策略总结:",
            f"菠菜资金：{fund_text}",
            f"资金最多连数: {cover_streak}连",
            f"{cover_streak}连所需本金: {fmt_wan(cover_required)}万",
            f"{cover_streak}连获得盈利: {fmt_wan(cover_profit)}万",
            "",
            "连数|倍率|下注| 盈利 |所需本金",
            "---|----|------|------|------",
        ]
    )

    for row in rows:
        multiplier_text = f"{row['multiplier']:.2f}".rstrip("0")
        if multiplier_text.endswith("."):
            multiplier_text += "0"
        row_text = (
            f"{str(row['streak']).center(3)}|"
            f"{multiplier_text.center(4)}|"
            f"{fmt_table_wan(row['bet']).center(6)}|"
            f"{fmt_table_wan(row['profit_if_win']).center(6)}|"
            f"{fmt_table_wan(row['cumulative_loss']).center(6)}"
        )
        lines.append(row_text)

    lines.append("```")
    return "\n".join(lines)


async def yc_command_handler_multiuser(
    client,
    event,
    args,
    user_ctx: UserContext,
    global_config: dict,
    auto_trigger: bool = False,
):
    """处理 yc 测算命令，支持 st 切换预设后自动触发。"""
    presets = user_ctx.presets
    rt = user_ctx.state.runtime

    params, preset_name, error_msg = _parse_yc_params(args, presets)
    if error_msg:
        await send_to_admin(
            client,
            _build_ops_card(
                "❌ 测算命令无法执行",
                summary="当前测算参数不完整或格式不正确。",
                note=error_msg,
                action="请执行 `yc [预设名]` 或 `yc [参数...]`，例如 `yc 5k`。",
            ),
            user_ctx,
            global_config,
        )
        return

    result_msg = _build_yc_result_message(
        params=params,
        preset_name=preset_name,
        current_fund=int(rt.get("gambling_fund", 0)),
        auto_trigger=auto_trigger,
    )
    await send_to_admin(client, result_msg, user_ctx, global_config)
    log_event(
        logging.INFO,
        'yc',
        '测算完成',
        user_id=user_ctx.user_id,
        preset=preset_name,
        auto_trigger=auto_trigger,
    )


async def fetch_balance(user_ctx: UserContext) -> int:
    zhuque = user_ctx.config.zhuque
    cookie = zhuque.get("cookie", "")
    csrf_token = zhuque.get("csrf_token", "") or zhuque.get("x_csrf", "")
    api_url = zhuque.get("api_url", "https://zhuque.in/api/user/getInfo?")
    
    if not cookie or not csrf_token:
        return 0
    
    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": csrf_token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 401:
                    user_ctx.set_runtime("balance_status", "auth_failed")
                    log_event(logging.ERROR, 'balance', '认证失败(401)，请更新 Cookie',
                              user_id=user_ctx.user_id)
                    return user_ctx.get_runtime("account_balance", 0)
                
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and data.get("status", 200) != 200:
                        log_event(logging.WARNING, 'balance', 'API返回错误',
                                  user_id=user_ctx.user_id, message=data.get("message"))
                        return user_ctx.get_runtime("account_balance", 0)
                    
                    balance = int(data.get("data", {}).get("bonus", 0))
                    user_ctx.set_runtime("balance_status", "success")
                    return balance
    except Exception as e:
        user_ctx.set_runtime("balance_status", "network_error")
        log_event(logging.ERROR, 'balance', '获取余额失败',
                  user_id=user_ctx.user_id, data=str(e))
    
    return 0
