import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace

from user_manager import UserContext, UserManager, trim_bet_sequence_log
from model_manager import ModelManager
import constants
import zq_multiuser as zm
import main_multiuser as mm


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if (path.name == "config.json" or path.name.endswith("_config.json")) and path.parent.parent.name == "users" and isinstance(data, dict):
        payload = json.loads(json.dumps(data, ensure_ascii=False))
        groups = payload.get("groups", {}) if isinstance(payload.get("groups", {}), dict) else {}
        notification = payload.get("notification", {}) if isinstance(payload.get("notification", {}), dict) else {}
        admin_console = payload.get("admin_console", {}) if isinstance(payload.get("admin_console", {}), dict) else {}
        if not admin_console:
            admin_chat = notification.get("admin_chat", groups.get("admin_chat", payload.get("telegram", {}).get("user_id", 0)))
            admin_console = {
                "mode": "telegram_id",
                "telegram_id": {"chat_id": admin_chat},
                "telegram_bot": {"bot_token": "", "chat_id": "", "allowed_sender_ids": []},
            }
            payload["admin_console"] = admin_console
        channels = notification.get("channels", {}) if isinstance(notification.get("channels", {}), dict) else {}
        if not channels:
            channels = {
                "iyuu": notification.get("iyuu", {"enable": False, "url": "", "token": ""}),
                "telegram_notify_bot": notification.get("tg_bot", {"enable": False, "bot_token": "", "chat_id": ""}),
            }
        payload["notification"] = {"channels": channels}
        data = payload
    if (path.name == "config.json" or path.name.endswith("_config.json")) and path.parent.parent.name == "users":
        canonical = path.parent / f"{path.parent.name}_config.json"
        canonical.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_user_context_user_id_fallback_numeric_dir(tmp_path):
    user_dir = tmp_path / "users" / "1001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "测试用户"},
            "telegram": {},
        },
    )

    ctx = UserContext(str(user_dir))
    assert ctx.user_id == 1001
    assert ctx.config.name == "测试用户"


def test_user_context_user_id_fallback_hash_dir(tmp_path):
    user_dir = tmp_path / "users" / "alpha_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Alpha"},
            "telegram": {},
        },
    )

    ctx = UserContext(str(user_dir))
    assert isinstance(ctx.user_id, int)
    assert ctx.user_id > 0


def test_user_context_prefers_user_dir_slug_for_logs(tmp_path):
    user_dir = tmp_path / "users" / "shuji"
    _write_json(
        user_dir / "shuji_config.json",
        {
            "account": {"name": "书记"},
            "telegram": {"user_id": 6002},
        },
    )

    ctx = UserContext(str(user_dir))

    assert ctx.account_slug == "shuji"
    assert zm._resolve_account_identity(user_ctx=ctx)["account_slug"] == "shuji"
    assert mm.register_main_user_log_identity(ctx) == "shuji"


def test_read_timing_config_defaults_match_v120():
    timing = zm._read_timing_config({})

    assert timing["prompt_wait_sec"] == 1.2
    assert timing["predict_timeout_sec"] == 8.0
    assert timing["click_interval_sec"] == 0.45
    assert timing["click_timeout_sec"] == 6.0


def test_format_dashboard_matches_status_html_layout(tmp_path):
    user_dir = tmp_path / "users" / "status_style_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态样式用户"},
            "telegram": {"user_id": 6003},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_on"] = True
    rt["current_preset_name"] = "yc5"
    rt["current_model_id"] = "deepseek-v3.2"
    rt["model_health_status"] = "ok"
    rt["model_last_ok_at"] = "2026-03-26 10:17:58"
    rt["account_balance"] = 2_411_500
    rt["balance_status"] = "success"
    rt["gambling_fund"] = 2_103_100
    rt["profit"] = 1_000_000
    rt["profit_stop"] = 2
    rt["period_profit"] = -5_875_900
    rt["earnings"] = -1_594_870
    rt["total"] = 1099
    rt["win_total"] = 491
    rt["continuous"] = 1
    rt["lose_stop"] = 12
    rt["lose_once"] = 2.8
    rt["lose_twice"] = 2.3
    rt["lose_three"] = 2.2
    rt["lose_four"] = 2.05
    rt["initial_amount"] = 5000
    ctx.state.history = [1, 0] * 20

    text = zm.format_dashboard(ctx)

    assert "<b>【 状态监控 】</b> 🟢 运行中" in text
    assert "<b>更新：</b>" in text
    assert "<b>版本：</b>" in text
    assert "<b>方案：</b> yc5" in text
    assert "🤖 模型状态：🟢 正常" in text
    assert "当前模型：deepseek-v3.2" in text
    assert "├ 计划下注：0.50 万" in text
    assert "<b>💰 资产总览</b>" in text
    assert "<b>📊 近期 40 次结果（由近及远）</b>" in text
    assert "<b>⚙️ 策略参数</b>" in text
    assert "<b>大模型：</b>" not in text
    assert "<pre>" not in text
    assert "<blockquote>" not in text
    assert "<code>" not in text


def test_get_current_predict_display_prefers_conclusion_line():
    rt = {
        "last_predict_info": (
            "🤖 决策依据\n"
            "├ 📊 100局： 长期分布接近均衡\n"
            "├ 🌊 40局： 数量偏小（小比大多 4 次）\n"
            "├ ⚡ 20局： 短线来回切换，交替还在延续\n"
            "├ 🧬 5局： 5位单跳 [0 1 0 1 0]，还在来回跳\n"
            "├ 🤖 大模型： 交替结构更强，先顺着节奏看\n"
            "└ 🎯 押注结论： 本局坚决押【小】"
        ),
        "bet_type": 1,
    }

    assert zm._get_current_predict_display(rt) == "小"


def test_format_dashboard_shows_strategy_watch_line_when_skip_streak_active(tmp_path):
    user_dir = tmp_path / "users" / "status_watch_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态观望用户"},
            "telegram": {"user_id": 6004},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_on"] = True
    rt["current_preset_name"] = "yc10"
    rt["current_model_id"] = "moonshotai/kimi-k2-instruct"
    rt["model_health_status"] = "ok"
    rt["stall_guard_sequence"] = 4
    rt["stall_guard_skip_streak"] = 2

    text = zm.format_dashboard(ctx)

    assert "策略观望：当前手位连续观望 2 次" in text


def test_format_dashboard_shows_lose_warning_lines_only_after_threshold(tmp_path):
    user_dir = tmp_path / "users" / "status_lose_warn_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态连输用户"},
            "telegram": {"user_id": 6014},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_on"] = True
    rt["warning_lose_count"] = 5
    rt["lose_count"] = 5

    text = zm.format_dashboard(ctx)

    assert text.startswith("连输：🟡 🟡 🟡 🟡 🟡\n")
    assert "<b>【 状态监控 】</b>" in text


def test_format_dashboard_hides_lose_warning_lines_before_threshold(tmp_path):
    user_dir = tmp_path / "users" / "status_no_lose_warn_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态未触发用户"},
            "telegram": {"user_id": 6015},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_on"] = True
    rt["warning_lose_count"] = 5
    rt["lose_count"] = 4

    text = zm.format_dashboard(ctx)

    assert "连输：🟡" not in text


def test_format_dashboard_shows_pending_release_notice(tmp_path):
    user_dir = tmp_path / "users" / "status_release_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态版本用户"},
            "telegram": {"user_id": 6006},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_on"] = True
    rt["current_preset_name"] = "5k"
    rt["release_latest_tag"] = "v9.9.9"

    text = zm.format_dashboard(ctx)

    assert "📦 新版本：v9.9.9（可更新）" in text


def test_format_dashboard_shows_model_probe_progress(tmp_path):
    user_dir = tmp_path / "users" / "status_probe_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态探测用户"},
            "telegram": {"user_id": 6005},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_on"] = False
    rt["current_preset_name"] = "yc10"
    rt["current_model_id"] = "qwen/qwen3-next-80b-a3b-instruct"
    rt["model_health_status"] = "down"
    rt["model_probe_active"] = True
    rt["model_probe_current_target"] = "moonshotai/kimi-k2-instruct"
    rt["model_probe_total"] = 5
    rt["model_probe_position"] = 2
    rt["model_last_fail_reason"] = "NVIDIA 接口超时"

    text = zm.format_dashboard(ctx)

    assert "🤖 模型状态：🟡 恢复探测中" in text
    assert "当前尝试：moonshotai/kimi-k2-instruct" in text
    assert "探测进度：2 / 5" in text
    assert "下次重试：约 3 秒后" in text


def test_format_dashboard_shows_continuous_exception_when_stat_fallback_disabled(tmp_path):
    user_dir = tmp_path / "users" / "status_model_wait_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态等待用户"},
            "telegram": {"user_id": 6007},
            "ai": {"enable_stat_fallback_bet": False},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["stat_fallback_bet_enabled"] = False
    rt["bet_on"] = True
    rt["model_health_status"] = "fallback"
    rt["model_fallback_streak"] = 2
    rt["current_model_id"] = "model-x"

    text = zm.format_dashboard(ctx)

    assert "🤖 模型状态：🟠 连续异常 2 次" in text


def test_legacy_ai_stat_fallback_setting_is_still_readable(tmp_path):
    user_dir = tmp_path / "users" / "legacy_mfb_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "旧配置用户"},
            "telegram": {"user_id": 6008},
            "ai": {"enable_stat_fallback_bet": False},
        },
    )

    ctx = UserContext(str(user_dir))
    ctx.state.runtime.pop("stat_fallback_bet_enabled", None)

    assert zm._is_stat_fallback_bet_enabled(ctx) is False


def test_user_manager_get_iflow_config_compatible_with_ai_key(tmp_path):
    users_dir = tmp_path / "users"
    config_dir = tmp_path / "config"
    users_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        config_dir / "global_config.json",
        {
            "ai": {"enabled": True, "base_url": "https://apis.iflow.cn/v1"},
        },
    )

    mgr = UserManager(users_dir=str(users_dir), config_dir=str(config_dir))
    mgr.load_all_users()
    cfg = mgr.get_iflow_config()
    assert cfg.get("enabled") is True
    assert "base_url" in cfg


def test_user_context_merges_global_common_and_user_private_config(tmp_path):
    users_dir = tmp_path / "users"
    config_dir = tmp_path / "config"
    users_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        config_dir / "global_config.json",
        {
            "groups": {"zq_group": [201], "zq_bot": 8},
            "zhuque": {"api_url": "https://zhuque.in/api/user/getInfo?"},
        },
    )
    _write_json(
        users_dir / "6001" / "6001_config.json",
        {
            "account": {"name": "合并用户"},
            "telegram": {"user_id": 6001},
            "zhuque": {"cookie": "c1", "x_csrf": "x1"},
            "notification": {
                "admin_chat": 6001,
                "iyuu": {"enable": True},
                "tg_bot": {"enable": True, "chat_id": "9"},
            },
            "ai": {
                "enabled": True,
                "base_url": "https://apis.iflow.cn/v1",
                "models": {"1": {"model_id": "m1", "enabled": True}},
            },
        },
    )

    mgr = UserManager(users_dir=str(users_dir), config_dir=str(config_dir))
    assert mgr.load_all_users() == 1

    ctx = mgr.get_user(6001)
    assert ctx is not None
    assert ctx.config.admin_console["mode"] == "telegram_id"
    assert ctx.config.admin_console["telegram_id"]["chat_id"] == 6001
    assert ctx.config.zhuque["api_url"] == "https://zhuque.in/api/user/getInfo?"
    assert ctx.config.zhuque["cookie"] == "c1"
    assert ctx.config.notification["channels"]["iyuu"]["enable"] is True
    assert ctx.config.ai["base_url"] == "https://apis.iflow.cn/v1"


def test_model_manager_apply_shared_config_uses_shared_chain():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "base_url": "https://apis.iflow.cn/v1",
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["2", "1"],
            }
        }
    )

    assert mgr.fallback_chain == ["2", "1"]
    assert mgr.get_model("1")["model_id"] == "model-1"
    assert mgr.get_model("2")["model_id"] == "model-2"


def test_model_manager_call_model_immediately_falls_back_to_next_ranked_model():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "base_url": "https://apis.iflow.cn/v1",
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            }
        }
    )

    async def fake_iflow(config, messages, **kwargs):
        if config["model_id"] == "model-1":
            return {"success": False, "error": "model-1 unavailable", "content": ""}
        return {"success": True, "error": "", "content": '{"prediction": 1}'}

    mgr._call_iflow = fake_iflow

    result = asyncio.run(
        mgr.call_model("model-1", [{"role": "user", "content": "ping"}], temperature=0.1, max_tokens=10)
    )

    assert result["success"] is True
    assert result["model_id"] == "model-2"
    assert result["requested_model_id"] == "model-1"
    assert result["fallback_used"] is True


def test_model_manager_apply_shared_config_detects_nvidia_provider_and_default_rate_limit():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "api_keys": ["nv-key"],
                "base_url": "https://integrate.api.nvidia.com/v1",
                "models": {
                    "1": {"model_id": "qwen/qwen3-next-80b-a3b-instruct", "enabled": True},
                },
                "fallback_chain": ["1"],
            }
        }
    )

    model = mgr.get_model("1")
    assert model is not None
    assert model["provider"] == "nvidia"
    assert model["rate_limit_rpm"] == 40


def test_model_manager_call_model_routes_nvidia_provider_via_openai_compatible_adapter():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "provider": "nvidia",
                "api_keys": ["nv-key"],
                "base_url": "https://integrate.api.nvidia.com/v1",
                "models": {
                    "1": {"model_id": "qwen/qwen3-next-80b-a3b-instruct", "enabled": True},
                },
                "fallback_chain": ["1"],
            }
        }
    )

    seen = {}

    async def fake_iflow(config, messages, **kwargs):
        seen["provider"] = config.get("provider")
        seen["rate_limit_rpm"] = config.get("rate_limit_rpm")
        return {"success": True, "error": "", "content": '{"prediction": 1}'}

    mgr._call_iflow = fake_iflow

    result = asyncio.run(
        mgr.call_model("1", [{"role": "user", "content": "ping"}], temperature=0.1, max_tokens=10)
    )

    assert result["success"] is True
    assert seen["provider"] == "nvidia"
    assert seen["rate_limit_rpm"] == 40


def test_model_manager_missing_requested_model_falls_back_to_chain_head():
    mgr = ModelManager()
    mgr.apply_shared_config(
        {
            "ai": {
                "enabled": True,
                "provider": "nvidia",
                "api_keys": ["nv-key"],
                "base_url": "https://integrate.api.nvidia.com/v1",
                "models": {
                    "1": {"model_id": "qwen/qwen3-next-80b-a3b-instruct", "enabled": True},
                    "2": {"model_id": "moonshotai/kimi-k2-instruct", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            }
        }
    )

    seen = []

    async def fake_iflow(config, messages, **kwargs):
        seen.append(config.get("model_id"))
        return {"success": True, "error": "", "content": '{"prediction": 1}'}

    mgr._call_iflow = fake_iflow

    result = asyncio.run(
        mgr.call_model("deepseek-v3", [{"role": "user", "content": "ping"}], temperature=0.1, max_tokens=10)
    )

    assert result["success"] is True
    assert result["model_id"] == "qwen/qwen3-next-80b-a3b-instruct"
    assert seen == ["qwen/qwen3-next-80b-a3b-instruct"]


def test_parse_analysis_result_insight_supports_skip_prediction():
    parsed = zm.parse_analysis_result_insight(
        '{"prediction":"SKIP","confidence":66,"reason":"证据冲突"}',
        default_prediction=1,
    )
    assert parsed["prediction"] == -1
    assert parsed["confidence"] == 66


def test_humanize_predict_reason_turns_english_into_plain_chinese():
    text = zm._humanize_predict_reason(
        "Chaos rhythm with weak evidence",
        "CHAOS_SWITCH",
        "PAIR_FORMATION",
        -1,
        25,
    )

    assert "盘面" in text or "节奏" in text
    assert "观望" in text
    assert "Chaos" not in text


def test_heal_stale_pending_bets_marks_orphan_none_records(tmp_path):
    user_dir = tmp_path / "users" / "heal_user_1"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "自愈用户"},
            "telegram": {"user_id": 7101},
            "groups": {"admin_chat": 7101},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = False
    ctx.state.bet_sequence_log = [
        {"bet_id": "b1", "result": "赢", "profit": 990},
        {"bet_id": "b2", "result": None, "profit": 0},
        {"bet_id": "b3", "result": None, "profit": None},
    ]

    result = zm.heal_stale_pending_bets(ctx)
    assert result["count"] == 2
    assert ctx.state.bet_sequence_log[1]["result"] == "异常未结算"
    assert ctx.state.bet_sequence_log[2]["result"] == "异常未结算"
    assert ctx.state.bet_sequence_log[2]["profit"] == 0
    assert rt["pending_bet_last_heal_count"] == 2
    assert rt["pending_bet_heal_total"] >= 2


def test_heal_stale_pending_bets_keeps_latest_when_bet_pending(tmp_path):
    user_dir = tmp_path / "users" / "heal_user_2"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "自愈用户2"},
            "telegram": {"user_id": 7102},
            "groups": {"admin_chat": 7102},
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    ctx.state.bet_sequence_log = [
        {"bet_id": "b1", "result": "输", "profit": -1000},
        {"bet_id": "b2", "result": None, "profit": None},
    ]

    result = zm.heal_stale_pending_bets(ctx)
    assert result["count"] == 0
    assert ctx.state.bet_sequence_log[-1]["result"] is None
    assert "pending_bet_last_heal_count" not in rt


def test_user_context_supports_hash_comments_in_config(tmp_path):
    user_dir = tmp_path / "users" / "commented"
    user_dir.mkdir(parents=True, exist_ok=True)
    config_text = """{
    # Telegram 登录参数
    "telegram": {
        "api_id": 123456,
        "api_hash": "abc123",
        "session_name": "demo",
        "user_id": 778899
    },
    # 管理员控制台
    "admin_console": {
        "mode": "telegram_id",
        "telegram_id": {"chat_id": 778899},
        "telegram_bot": {"bot_token": "", "chat_id": "", "allowed_sender_ids": []}
    },
    # 账号信息
    "account": {"name": "注释用户"} # 行尾注释
}
"""
    (user_dir / "comment_user_config.json").write_text(config_text, encoding="utf-8")
    ctx = UserContext(str(user_dir))
    assert ctx.user_id == 778899
    assert ctx.config.name == "注释用户"


def test_zq_log_event_includes_account_prefix_and_business_category(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "log_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Musk Xu"},
            "telegram": {"user_id": 7001},
        },
    )
    ctx = UserContext(str(user_dir))
    zm.register_user_log_identity(ctx)

    captured = {}

    def fake_log(level, message, extra=None):
        captured["level"] = level
        captured["message"] = message
        captured["extra"] = extra or {}

    monkeypatch.setattr(zm.logger, "log", fake_log)
    zm.log_event(logging.INFO, "user_cmd", "处理用户命令", "ok", user_id=7001)

    assert captured["level"] == logging.INFO
    assert captured["extra"]["account_tag"] == "【ydx-log_user】"
    assert captured["extra"]["category"] == "business"
    assert captured["extra"]["user_id"] == "7001"


def test_zq_log_event_warning_level_goes_to_warning_category(monkeypatch):
    captured = {}

    def fake_log(level, message, extra=None):
        captured["level"] = level
        captured["extra"] = extra or {}

    monkeypatch.setattr(zm.logger, "log", fake_log)
    zm.log_event(logging.ERROR, "start", "用户启动失败", "fail", user_id=9001)

    assert captured["level"] == logging.ERROR
    assert captured["extra"]["category"] == "warning"
    assert captured["extra"]["account_tag"] == "【ydx-user-9001】"


def test_user_context_migrates_risk_default_switches_from_legacy_runtime(tmp_path):
    user_dir = tmp_path / "users" / "risk_migrate_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "迁移用户"},
            "telegram": {"user_id": 6122},
        },
    )
    _write_json(
        user_dir / "state.json",
        {
            "history": [],
            "bet_type_history": [],
            "predictions": [],
            "bet_sequence_log": [],
            "runtime": {
                "risk_base_enabled": False,
                "risk_deep_enabled": True,
            },
        },
    )

    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    assert rt["risk_base_enabled"] is False
    assert rt["risk_deep_enabled"] is True
    assert rt["risk_base_default_enabled"] is False
    assert rt["risk_deep_default_enabled"] is True


def test_user_context_refreshes_builtin_presets_but_keeps_custom(tmp_path):
    user_dir = tmp_path / "users" / "preset_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "预设用户"},
            "telegram": {"user_id": 6123},
        },
    )
    _write_json(
        user_dir / "presets.json",
        {
            "yc05": ["1", "13", "3", "2.1", "2.1", "2.05", "500"],
            "my_custom": ["1", "6", "2.2", "2.1", "2.0", "2.0", "800"],
        },
    )

    ctx = UserContext(str(user_dir))
    assert "yc05" not in ctx.presets
    assert ctx.presets["5k"] == constants.PRESETS["5k"]
    assert ctx.presets["my_custom"] == ["1", "6", "2.2", "2.1", "2.0", "2.0", "800"]

    saved_presets = json.loads((user_dir / "presets.json").read_text(encoding="utf-8"))
    assert "yc05" not in saved_presets
    assert saved_presets["5k"] == constants.PRESETS["5k"]
    assert saved_presets["my_custom"] == ["1", "6", "2.2", "2.1", "2.0", "2.0", "800"]


def test_main_multiuser_settle_regex_is_strict():
    source = Path("main_multiuser.py").read_text(encoding="utf-8")
    assert 'pattern=r"已结算: 结果为 (\\d+) (大|小)"' in source

    pattern = re.compile(r"已结算: 结果为 (\d+) (大|小)")
    assert pattern.search("已结算: 结果为 12 大")
    assert pattern.search("已结算: 结果为 8 小")
    assert pattern.search("已结算: 结果为 9 |") is None


def test_main_multiuser_session_lock_prevents_duplicate_acquire(tmp_path):
    user_dir = tmp_path / "users" / "lock_user"
    user_dir.mkdir(parents=True, exist_ok=True)

    ctx1 = SimpleNamespace(
        user_dir=str(user_dir),
        user_id=9001,
        config=SimpleNamespace(telegram={"session_name": "dup"}),
    )
    ctx2 = SimpleNamespace(
        user_dir=str(user_dir),
        user_id=9002,
        config=SimpleNamespace(telegram={"session_name": "dup"}),
    )

    assert mm._acquire_session_lock(ctx1) is True
    try:
        assert mm._acquire_session_lock(ctx2) is False
        mm._release_session_lock(ctx1)
        assert mm._acquire_session_lock(ctx2) is True
    finally:
        mm._release_session_lock(ctx1)
        mm._release_session_lock(ctx2)


def test_main_log_event_includes_account_prefix(monkeypatch):
    captured = {}
    fake_ctx = SimpleNamespace(
        user_id=8801,
        config=SimpleNamespace(name="Musk Xu"),
        account_slug="xu",
    )
    mm.register_main_user_log_identity(fake_ctx)

    def fake_log(level, message, extra=None):
        captured["level"] = level
        captured["extra"] = extra or {}

    monkeypatch.setattr(mm.logger, "log", fake_log)
    mm.log_event(logging.INFO, "start", "用户启动成功", "ok", user_id=8801)

    assert captured["level"] == logging.INFO
    assert captured["extra"]["account_tag"] == "【ydx-xu】"
    assert captured["extra"]["category"] in {"runtime", "business"}


def test_start_user_sends_startup_ready_notice(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "startup_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "启动通知用户"},
            "telegram": {
                "user_id": 8802,
                "api_id": 123,
                "api_hash": "hash",
                "session_name": "startup_user",
            },
            "groups": {"zq_group": [1], "zq_bot": [2]},
            "admin_console": {
                "mode": "telegram_id",
                "telegram_id": {"chat_id": 8802},
                "telegram_bot": {"bot_token": "", "chat_id": "", "allowed_sender_ids": []},
            },
            "notification": {
                "channels": {
                    "iyuu": {"enable": False},
                    "telegram_notify_bot": {"enable": True, "bot_token": "token", "chat_id": "chat"},
                },
            },
        },
    )
    ctx = UserContext(str(user_dir))

    sent = []

    class DummyClient:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=8802, id=len(sent))

    async def fake_create_client(user_ctx, global_cfg):
        return DummyClient()

    async def fake_fetch_account_balance(user_ctx):
        return 12_340_000

    monkeypatch.setattr(mm, "_acquire_session_lock", lambda _ctx: True)
    monkeypatch.setattr(mm, "create_client", fake_create_client)
    monkeypatch.setattr(mm, "register_handlers", lambda client, user_ctx, global_cfg: None)
    monkeypatch.setattr(mm, "fetch_account_balance", fake_fetch_account_balance)
    monkeypatch.setattr(mm, "register_main_user_log_identity", lambda _ctx: "startup-user")
    monkeypatch.setattr(mm, "_resolve_admin_chat", lambda _ctx: 8802)
    monkeypatch.setattr(zm, "register_user_log_identity", lambda _ctx: "startup-user")
    monkeypatch.setattr(zm, "heal_stale_pending_bets", lambda _ctx: {"count": 0, "items": []})
    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "get_software_version_text", lambda: "v1.2.3(test)")

    client = asyncio.run(mm.start_user(ctx, {}))

    assert client is not None
    assert sent
    assert sent[-1][0] == "startup_ready"
    assert "✅ 脚本启动成功" in sent[-1][1]
    assert "重启日期：" in sent[-1][1]
    assert "脚本版本：v1.2.3(test)" in sent[-1][1]
    assert "账户余额：1234.00 万" in sent[-1][1]
    assert "菠菜资金：2500.00 万" in sent[-1][1]


def test_send_message_v2_admin_bot_renders_markdown_title_as_html(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "admin_bot_render_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Bot管理用户"},
            "telegram": {"user_id": 8803},
            "admin_console": {
                "mode": "telegram_bot",
                "telegram_id": {"chat_id": 8803},
                "telegram_bot": {
                    "bot_token": "token",
                    "chat_id": "8803",
                    "allowed_sender_ids": [8803]
                }
            },
            "notification": {"channels": {"iyuu": {"enable": False}, "telegram_notify_bot": {"enable": False}}},
        },
    )
    ctx = UserContext(str(user_dir))
    payloads = []

    def fake_post(url, data=None, json=None, timeout=5):
        payloads.append({"url": url, "json": json, "data": data})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 9}})

    monkeypatch.setattr(zm.requests, "post", fake_post)

    asyncio.run(
        zm.send_message_v2(
            SimpleNamespace(),
            "info",
            "🎯 **测试标题** 🎯\n\n本局下注指令已发送。",
            ctx,
            {},
            parse_mode="markdown",
        )
    )

    assert payloads
    payload = payloads[-1]["json"]
    assert payload["parse_mode"] == "HTML"
    assert "<b>测试标题</b>" in payload["text"]
    assert "reply_markup" not in payload


def test_start_user_initializes_admin_bot_menu(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "admin_bot_menu_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "Bot菜单用户"},
            "telegram": {
                "user_id": 8804,
                "api_id": 123,
                "api_hash": "hash",
                "session_name": "admin_bot_menu_user",
            },
            "groups": {"zq_group": [1], "zq_bot": [2]},
            "admin_console": {
                "mode": "telegram_bot",
                "telegram_id": {"chat_id": 8804},
                "telegram_bot": {
                    "bot_token": "token",
                    "chat_id": "8804",
                    "allowed_sender_ids": [8804]
                }
            },
            "notification": {"channels": {"iyuu": {"enable": False}, "telegram_notify_bot": {"enable": False}}},
        },
    )
    ctx = UserContext(str(user_dir))
    calls = []

    class DummyClient:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

    async def fake_create_client(user_ctx, global_cfg):
        return DummyClient()

    async def fake_fetch_account_balance(user_ctx):
        return 12_340_000

    async def fake_bot_api_request(bot_token, method, payload=None, timeout=30):
        calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 10}}
        return {"ok": True, "result": True}

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(mm, "_acquire_session_lock", lambda _ctx: True)
    monkeypatch.setattr(mm, "create_client", fake_create_client)
    monkeypatch.setattr(mm, "register_handlers", lambda client, user_ctx, global_cfg: None)
    monkeypatch.setattr(mm, "fetch_account_balance", fake_fetch_account_balance)
    monkeypatch.setattr(mm, "register_main_user_log_identity", lambda _ctx: "bot-menu-user")
    monkeypatch.setattr(zm, "register_user_log_identity", lambda _ctx: "bot-menu-user")
    monkeypatch.setattr(zm, "heal_stale_pending_bets", lambda _ctx: {"count": 0, "items": []})
    monkeypatch.setattr(mm, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(mm.asyncio, "create_task", fake_create_task)

    client = asyncio.run(mm.start_user(ctx, {}))

    assert client is not None
    methods = [method for method, _ in calls]
    assert "setMyCommands" in methods
    assert "setChatMenuButton" in methods


def test_user_isolation_between_two_contexts(tmp_path):
    users_dir = tmp_path / "users"
    config_dir = tmp_path / "config"
    _write_json(config_dir / "global_config.json", {"groups": {"zq_group": [1]}})

    _write_json(users_dir / "1001" / "config.json", {"account": {"name": "U1"}, "telegram": {"user_id": 1001}})
    _write_json(users_dir / "1002" / "config.json", {"account": {"name": "U2"}, "telegram": {"user_id": 1002}})

    mgr = UserManager(users_dir=str(users_dir), config_dir=str(config_dir))
    assert mgr.load_all_users() == 2

    u1 = mgr.get_user(1001)
    u2 = mgr.get_user(1002)
    assert u1 is not None and u2 is not None

    u1.set_runtime("bet_amount", 12345)
    u2.set_runtime("bet_amount", 54321)

    assert u1.get_runtime("bet_amount") == 12345
    assert u2.get_runtime("bet_amount") == 54321


def test_user_state_save_concurrent_no_corruption(tmp_path):
    user_dir = tmp_path / "users" / "2001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "并发用户"},
            "telegram": {"user_id": 2001},
        },
    )
    ctx = UserContext(str(user_dir))

    def worker(i):
        ctx.set_runtime("counter", i)
        ctx.save_state()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state_path = user_dir / "state.json"
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert "runtime" in loaded
    assert isinstance(loaded["runtime"], dict)
    assert "counter" in loaded["runtime"]


def test_send_message_returns_admin_message_object(tmp_path):
    user_dir = tmp_path / "users" / "3001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "消息用户"},
            "telegram": {"user_id": 3001},
            "groups": {"admin_chat": 3001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=88)

    message = asyncio.run(
        zm.send_message(
            DummyClient(),
            "admin",
            "hello",
            ctx,
            {},
        )
    )
    assert message is not None
    assert message.id == 88


def test_process_bet_on_parses_history_and_places_bet(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "下注用户"},
            "telegram": {"user_id": 4001},
            "groups": {"admin_chat": 4001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "test"
        user_ctx.state.predictions.append(1)
        return 1

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["1", "1"] + (["0", "1"] * 19)))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 1
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert len(ctx.state.history) >= 40
    assert rt.get("bet") is True
    assert len(ctx.state.bet_sequence_log) == 1
    assert rt.get("current_bet_seq", 1) >= 2
    assert any("押注方向：大" in message for message in sent_messages)


def test_process_bet_on_allows_short_history_like_master(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4002"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "短历史用户"},
            "telegram": {"user_id": 4002},
            "groups": {"admin_chat": 4002},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "test-short-history"
        user_ctx.state.predictions.append(1)
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            self.message = SimpleNamespace(message="[近 40 次结果][由近及远][0 小 1 大] 1 0 1")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 1
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert len(ctx.state.history) == 3
    assert rt.get("bet") is True
    assert len(ctx.state.bet_sequence_log) == 1


def test_process_bet_on_overrides_model_with_alternation_break_same_side(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4010"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "交替增强用户"},
            "telegram": {"user_id": 4010},
            "groups": {"admin_chat": 4010},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_amount"] = 500
    rt["lose_count"] = 0

    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "模型原判断押大"
        user_ctx.state.runtime["last_predict_source"] = "model"
        user_ctx.state.runtime["last_predict_tag"] = "SINGLE_JUMP"
        user_ctx.state.runtime["last_predict_confidence"] = 83
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            self.message = SimpleNamespace(message="[近 40 次结果][由近及远][0 小 1 大] 0 1 0 1 0 1 1 1")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 101
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["bet"] is True
    assert rt["bet_type"] == 0
    assert rt["last_predict_source"] == "alternation_break"
    assert "├ 🧬 5局：" in rt["last_predict_info"]
    assert "├ 🤖 大模型： 交替拉满了，直接按打断规则走" in rt["last_predict_info"]
    assert "└ 🎯 押注结论： 本局坚决押【小】" in rt["last_predict_info"]
    assert any("押注方向：小" in message for message in sent_messages)


def test_process_bet_on_alternation_break_can_override_skip(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4011"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "交替跳过用户"},
            "telegram": {"user_id": 4011},
            "groups": {"admin_chat": 4011},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["bet_amount"] = 500
    rt["lose_count"] = 0

    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "模型建议观望"
        user_ctx.state.runtime["last_predict_source"] = "model_skip"
        user_ctx.state.runtime["last_predict_tag"] = "SINGLE_JUMP"
        user_ctx.state.runtime["last_predict_confidence"] = 61
        return -1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=1)

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            self.message = SimpleNamespace(message="[近 40 次结果][由近及远][0 小 1 大] 1 0 1 0 1 0 0 0")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 102
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["bet"] is True
    assert rt["bet_type"] == 1
    assert rt["last_predict_source"] == "alternation_break"
    assert "├ 🧬 5局：" in rt["last_predict_info"]
    assert "├ 🤖 大模型： 交替拉满了，直接按打断规则走" in rt["last_predict_info"]
    assert "└ 🎯 押注结论： 本局坚决押【大】" in rt["last_predict_info"]
    assert any("押注方向：大" in message for message in sent_messages)


def test_process_bet_on_recovers_when_source_message_id_invalid(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "4003"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回溯点击用户"},
            "telegram": {"user_id": 4003},
            "groups": {"admin_chat": 4003, "zq_bot": 9001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["stop_count"] = 0
    rt["initial_amount"] = 500
    rt["bet_amount"] = 500
    rt["lose_count"] = 0
    rt["win_count"] = 0

    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "test-recover"
        user_ctx.state.predictions.append(1)
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=1, id=len(sent_messages))

    async def fake_delete_later(*args, **kwargs):
        return None

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "delete_later", fake_delete_later)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 1
            self.id = 100
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)
            raise Exception("The specified message ID is invalid or you can't do that operation on such message (caused by GetBotCallbackAnswerRequest)")

    class DummyFreshMsg:
        def __init__(self):
            self.sender_id = 9001
            self.reply_markup = object()
            self.message = "[近 40 次结果][由近及远][0 小 1 大] 1 0 1 0"
            self.raw_text = self.message
            self.id = 101
            self.clicked = []

        async def click(self, data):
            self.clicked.append(data)

    fresh_msg = DummyFreshMsg()

    class DummyClient:
        def __init__(self):
            self._fresh_msg = fresh_msg

        def iter_messages(self, chat_id, limit=20):
            async def _gen():
                yield self._fresh_msg
            return _gen()

    event = DummyEvent()
    client = DummyClient()
    asyncio.run(zm.process_bet_on(client, event, ctx, {}))

    assert fresh_msg.clicked  # 使用回溯消息完成点击
    assert len(ctx.state.bet_sequence_log) == 1
    assert all("押注出错" not in msg for msg in sent_messages)


def test_user_context_migrates_legacy_state_when_history_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    legacy_user_id = 500099

    user_dir = tmp_path / "users" / "xu"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "迁移用户"},
            "telegram": {"user_id": legacy_user_id},
        },
    )
    _write_json(user_dir / "state.json", {"history": [], "runtime": {}})
    (tmp_path / "config.py").write_text(f"user = {legacy_user_id}\n", encoding="utf-8")

    legacy_state = {
        "history": [0, 1] * 30,  # 60条
        "bet_type_history": [0, 1] * 30,
        "predictions": [1, 0] * 30,
        "bet_sequence_log": [],
        "state": {"current_model_id": "qwen3-coder-plus", "bet_amount": 500},
    }
    _write_json(tmp_path / "state.json", legacy_state)

    ctx = UserContext(str(user_dir))
    assert len(ctx.state.history) >= 40


def test_send_message_v2_routes_and_account_prefix(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5001"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "路由用户"},
            "telegram": {"user_id": 5001},
            "groups": {"admin_chat": 5001},
            "notification": {
                "iyuu": {"enable": True, "url": "https://iyuu.test/send"},
                "tg_bot": {"enable": True, "bot_token": "token", "chat_id": "chat"},
            },
        },
    )
    ctx = UserContext(str(user_dir))

    requests_payloads = []

    def fake_post(url, data=None, json=None, timeout=5):
        requests_payloads.append({"url": url, "data": data, "json": json})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(zm.requests, "post", fake_post)

    class DummyClient:
        def __init__(self):
            self.messages = []

        async def send_message(self, target, message, parse_mode=None):
            self.messages.append((target, message))
            return SimpleNamespace(chat_id=target, id=7)

    client = DummyClient()
    asyncio.run(
        zm.send_message_v2(
            client,
            "lose_streak",
            "⚠️ 3 连输告警 ⚠️\n\n当前链路已进入高关注状态，请重点关注下一手与账户余额变化。\n🔢 时间：03月25日 第 5 轮第 24 次\n📋 预设名称：yc20\n😀 连续押注：3 次\n⚡ 押注方向：小\n💵 押注本金：13.10 万\n💰 累计损失：20.75 万\n💰 账户余额：1118.75 万\n💰 菠菜余额：1111.24 万",
            ctx,
            {},
            title="标题",
            desp="⚠️ 3 连输告警 ⚠️\n\n当前链路已进入高关注状态，请重点关注下一手与账户余额变化。\n🔢 时间：03月25日 第 5 轮第 24 次\n📋 预设名称：yc20\n😀 连续押注：3 次\n⚡ 押注方向：小\n💵 押注本金：13.10 万\n💰 累计损失：20.75 万\n💰 账户余额：1118.75 万\n💰 菠菜余额：1111.24 万",
        )
    )

    assert client.messages
    assert "⚠️ 3 连输告警 ⚠️" in client.messages[0][1]
    assert len(requests_payloads) == 2
    iyuu_payload = next(item for item in requests_payloads if "iyuu" in item["url"])
    tg_payload = next(item for item in requests_payloads if "api.telegram.org" in item["url"])
    assert iyuu_payload["data"]["desp"].startswith("【账号：路由用户】\n\n")
    assert tg_payload["json"]["text"].startswith("【账号：路由用户】\n\n")
    assert "💰 累计损失：20.75 万" in tg_payload["json"]["text"]


def test_send_message_v2_lose_end_priority_keeps_account_prefix(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5011"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回补用户"},
            "telegram": {"user_id": 5011},
            "groups": {"admin_chat": 5011},
            "notification": {
                "iyuu": {"enable": True, "url": "https://iyuu.test/send"},
                "tg_bot": {"enable": True, "bot_token": "token", "chat_id": "chat"},
            },
        },
    )
    ctx = UserContext(str(user_dir))

    requests_payloads = []

    def fake_post(url, data=None, json=None, timeout=5):
        requests_payloads.append({"url": url, "data": data, "json": json})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(zm.requests, "post", fake_post)

    class DummyClient:
        def __init__(self):
            self.messages = []

        async def send_message(self, target, message, parse_mode=None):
            self.messages.append((target, message))
            return SimpleNamespace(chat_id=target, id=8)

    client = DummyClient()
    asyncio.run(
        zm.send_message_v2(
            client,
            "lose_end",
            "✅ 3 连输已终止！ ✅\n\n本轮回补已经结束，系统已回写收益与当前余额。\n🔢 时间：03月25日 第 5 轮第 22 次 至 第 25 次\n📋 预设名称：yc20\n😀 连续押注：4 次\n⚠️ 本局连输：3 次\n💰 本局盈利：8.06 万\n💰 账户余额：1147.55 万\n💰 菠菜资金剩余：1140.05 万",
            ctx,
            {},
            title="标题",
            desp="✅ 3 连输已终止！ ✅\n\n本轮回补已经结束，系统已回写收益与当前余额。\n🔢 时间：03月25日 第 5 轮第 22 次 至 第 25 次\n📋 预设名称：yc20\n😀 连续押注：4 次\n⚠️ 本局连输：3 次\n💰 本局盈利：8.06 万\n💰 账户余额：1147.55 万\n💰 菠菜资金剩余：1140.05 万",
        )
    )

    assert client.messages
    assert "✅ 3 连输已终止！ ✅" in client.messages[0][1]
    iyuu_payload = next(item for item in requests_payloads if "iyuu" in item["url"])
    tg_payload = next(item for item in requests_payloads if "api.telegram.org" in item["url"])
    assert iyuu_payload["data"]["desp"].startswith("【账号：回补用户】\n\n")
    assert tg_payload["json"]["text"].startswith("【账号：回补用户】\n\n")
    assert "💰 菠菜资金剩余：1140.05 万" in tg_payload["json"]["text"]


def test_build_yc_result_message_uses_codeblock_table():
    params = {
        "continuous": 1,
        "lose_stop": 13,
        "lose_once": 3.0,
        "lose_twice": 2.1,
        "lose_three": 2.1,
        "lose_four": 2.05,
        "initial_amount": 3000,
    }
    msg = zm._build_yc_result_message(params, "yc_demo", current_fund=30_000_000, auto_trigger=False)

    assert msg.startswith("```")
    assert "🎯 策略参数" in msg
    assert "策略命令: 1 13 3.0 2.1 2.1 2.05 3000" in msg
    assert "🎯 策略总结:" in msg
    assert "资金最多连数:" in msg
    assert "连数|倍率|下注| 盈利 |所需本金" in msg
    assert " 15|" in msg
    assert msg.count("```") == 2


def test_process_settle_no_longer_auto_sends_ydx(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5002"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算用户"},
            "telegram": {"user_id": 5002},
            "groups": {"admin_chat": 5002, "zq_group": [101, 102]},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.runtime["open_ydx"] = True
    ctx.state.runtime["bet"] = False

    async def fake_fetch_balance(user_ctx):
        return 123456

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5002, id=99)

    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    class DummyClient:
        def __init__(self):
            self.sent = []

        async def send_message(self, target, message, parse_mode=None):
            self.sent.append((target, message))
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(message=SimpleNamespace(message="已结算: 结果为 8 小"))
    client = DummyClient()
    asyncio.run(zm.process_settle(client, event, ctx, {}))

    ydx_messages = [msg for msg in client.sent if msg[1] == "/ydx"]
    assert ydx_messages == []
    assert ctx.state.history[-1] == 0


def test_check_bet_status_can_resume_when_fund_sufficient(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5003"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "恢复用户"},
            "telegram": {"user_id": 5003},
            "groups": {"admin_chat": 5003},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["stop_count"] = 0
    rt["bet"] = False
    rt["gambling_fund"] = 2_000_000
    rt["bet_amount"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0

    sent = {}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["message"] = message
        return SimpleNamespace(chat_id=5003, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    asyncio.run(zm.check_bet_status(SimpleNamespace(), ctx, {}))

    # 恢复可下注状态时不应提前标记为“已下注”，避免结算时序误判。
    assert rt["bet"] is False
    assert rt["pause_count"] == 0
    assert "恢复可下注状态" in sent["message"]


def test_pause_command_sets_manual_pause_and_blocks_bet_on(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5005"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "暂停用户"},
            "telegram": {"user_id": 5005},
            "groups": {"admin_chat": 5005},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5005, id=1)

    async def fake_sleep(*args, **kwargs):
        return None

    async def fail_predict(*args, **kwargs):
        raise AssertionError("predict should not run while manual pause is active")

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm, "predict_next_bet_core", fail_predict)

    cmd_event = SimpleNamespace(raw_text="pause", chat_id=5005, id=10)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    assert rt["manual_pause"] is True
    assert rt["bet_on"] is False
    assert rt["bet"] is False

    class DummyEvent:
        def __init__(self):
            history = " ".join((["0", "1"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 5005
            self.id = 11

    asyncio.run(zm.process_bet_on(SimpleNamespace(), DummyEvent(), ctx, {}))


def test_check_bet_status_does_not_resume_when_manual_pause(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5006"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "手动暂停用户"},
            "telegram": {"user_id": 5006},
            "groups": {"admin_chat": 5006},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = True
    rt["stop_count"] = 0
    rt["bet"] = False
    rt["gambling_fund"] = 2_000_000
    rt["bet_amount"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0

    sent = {"called": False}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["called"] = True
        return SimpleNamespace(chat_id=5006, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    asyncio.run(zm.check_bet_status(SimpleNamespace(), ctx, {}))

    assert rt["bet"] is False
    assert sent["called"] is False


def test_process_settle_lose_warning_matches_master_style(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5004"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "告警用户"},
            "telegram": {"user_id": 5004},
            "groups": {"admin_chat": 5004},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小
    rt["bet_amount"] = 500
    rt["warning_lose_count"] = 1
    rt["bet_sequence_count"] = 1
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    rt["current_round"] = 1
    rt["current_bet_seq"] = 2
    rt["current_preset_name"] = "yc10"
    ctx.state.bet_sequence_log = [{"bet_id": "20260223_1_1", "profit": None}]

    captured = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        captured.append((msg_type, message, parse_mode))
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5004, id=12)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    lose_streak_messages = [message for msg_type, message, _ in captured if msg_type == "lose_streak"]
    assert lose_streak_messages
    assert "⚠️ 1 连输告警" in lose_streak_messages[0]
    assert "第 1 轮第 1 次" in lose_streak_messages[0]
    assert "📋 预设名称：yc10" in lose_streak_messages[0]
    assert "💰 账户余额：" in lose_streak_messages[0]
    assert "💰 累计损失：" in lose_streak_messages[0]
    assert "🤖 当局 AI 预测提示" not in lose_streak_messages[0]


def test_process_settle_lose_end_message_contains_balance_lines(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5007"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回补用户"},
            "telegram": {"user_id": 5007},
            "groups": {"admin_chat": 5007},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 1000
    rt["warning_lose_count"] = 1
    rt["lose_count"] = 3
    rt["lose_notify_pending"] = True
    rt["lose_start_info"] = {"round": 1, "seq": 5, "fund": 24_566_390}
    rt["current_round"] = 1
    rt["current_bet_seq"] = 10
    rt["current_preset_name"] = "yc10"
    rt["account_balance"] = 24_634_900
    rt["gambling_fund"] = 24_567_390
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260224_1_6", "profit": -1_000, "result": "输"},
        {"bet_id": "20260224_1_7", "profit": -2_000, "result": "输"},
        {"bet_id": "20260224_1_8", "profit": -3_000, "result": "输"},
        {"bet_id": "20260224_1_9", "profit": None, "result": None},
    ]

    captured = {}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        if msg_type == "lose_end":
            captured["message"] = message
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5007, id=1)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    msg = captured["message"]
    assert "✅ 3 连输已终止！ ✅" in msg
    assert "第 1 轮第 5 次 至 第 9 次" in msg
    assert "📋 预设名称：yc10" in msg
    assert "😀 连续押注：4 次" in msg
    assert "⚠️ 本局连输：3 次" in msg
    assert "💰 本局盈利：-0.50 万" in msg
    assert "💰 账户余额：2463.49 万" in msg
    assert "💰 菠菜资金剩余：2456.84 万" in msg


def test_process_settle_skips_stale_lose_end_when_old_lose_count_zero(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5022"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "连输脏状态用户"},
            "telegram": {"user_id": 5022},
            "groups": {"admin_chat": 5022},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 1000
    rt["warning_lose_count"] = 3
    rt["lose_count"] = 0
    rt["lose_notify_pending"] = True
    rt["lose_start_info"] = {"round": 2, "seq": 56, "fund": 9_999_999}
    rt["current_round"] = 1
    rt["current_bet_seq"] = 1
    rt["current_preset_name"] = "yc05"
    rt["account_balance"] = 315_300
    rt["gambling_fund"] = 314_800
    ctx.state.bet_sequence_log = [{"bet_id": "20260228_1_1", "profit": None}]

    sent_types = []
    sent_msgs = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent_types.append(msg_type)
        sent_msgs.append(message)
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_msgs.append(message)
        return SimpleNamespace(chat_id=5022, id=len(sent_msgs))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=45001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert "lose_end" not in sent_types
    assert not any("0 连输已终止" in m for m in sent_msgs)
    assert rt["lose_notify_pending"] is False
    assert rt["lose_start_info"] == {}


def test_process_settle_skips_lose_end_when_range_is_invalid(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5023"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "连输区间异常用户"},
            "telegram": {"user_id": 5023},
            "groups": {"admin_chat": 5023},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1  # 押大，下面开大 -> 赢
    rt["bet_amount"] = 1000
    rt["warning_lose_count"] = 3
    rt["lose_count"] = 3
    rt["lose_notify_pending"] = True
    rt["lose_start_info"] = {"round": 2, "seq": 56, "fund": 9_999_999}
    rt["current_round"] = 1
    rt["current_bet_seq"] = 1
    rt["current_preset_name"] = "yc05"
    rt["account_balance"] = 315_300
    rt["gambling_fund"] = 314_800
    ctx.state.bet_sequence_log = [{"bet_id": "20260228_1_1", "profit": None}]

    sent_types = []
    sent_msgs = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent_types.append(msg_type)
        sent_msgs.append(message)
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_msgs.append(message)
        return SimpleNamespace(chat_id=5023, id=len(sent_msgs))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=45002, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert "lose_end" not in sent_types
    assert not any("连输已终止" in m for m in sent_msgs)
    assert rt["lose_notify_pending"] is False
    assert rt["lose_start_info"] == {}


def test_reconcile_bet_runtime_from_log_ignores_stale_pending_entries(tmp_path):
    user_dir = tmp_path / "users" / "5091"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "脏挂单修复用户"},
            "telegram": {"user_id": 5091},
            "groups": {"admin_chat": 5091},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["initial_amount"] = 50_000
    rt["bet"] = False
    rt["bet_sequence_count"] = 6
    rt["lose_count"] = 5
    rt["bet_amount"] = 2_145_500
    rt["lose_once"] = 2.1
    rt["lose_twice"] = 2.1
    rt["lose_three"] = 2.1
    rt["lose_four"] = 2.05
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_25", "amount": 106_000, "result": "赢", "profit": 104_940},
        {"bet_id": "20260312_1_26", "amount": 50_000, "result": "输", "profit": -50_000},
        {"bet_id": "20260312_1_27", "amount": 106_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_28", "amount": 225_000, "result": "输", "profit": -225_000},
        {"bet_id": "20260312_1_29", "amount": 477_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_30", "amount": 1_011_500, "result": None, "profit": 0},
    ]

    healed = zm.heal_stale_pending_bets(ctx)
    summary = zm.reconcile_bet_runtime_from_log(ctx)

    assert healed["count"] == 3
    assert summary["continuous_count"] == 2
    assert summary["lose_count"] == 2
    assert rt["bet_sequence_count"] == 2
    assert rt["lose_count"] == 2
    assert rt["bet_amount"] == 225_000
    assert zm.calculate_bet_amount(rt) == 477_000


def test_build_pending_bet_heal_notice_contains_reconciled_status():
    rt = {
        "initial_amount": 50_000,
        "bet_sequence_count": 2,
        "lose_count": 2,
        "bet_amount": 225_000,
        "lose_once": 2.1,
        "lose_twice": 2.1,
        "lose_three": 2.1,
        "lose_four": 2.05,
    }
    notice = zm.build_pending_bet_heal_notice(
        {"count": 3, "items": ["20260312_1_27", "20260312_1_29", "20260312_1_30"]},
        {"continuous_count": 2, "lose_count": 2},
        rt,
    )

    assert "已修正历史异常挂单" in notice
    assert "修复条数：3" in notice
    assert "当前连续押注：2 次" in notice
    assert "当前连输：2 次" in notice
    assert "下一手预计下注：47.70 万" in notice


def test_trim_bet_sequence_log_keeps_new_chain_after_res_bet_rollover():
    runtime = {"bet_reset_log_index": 5000}
    logs = [{"bet_id": f"b{i}", "result": "输", "profit": -1} for i in range(5000)]

    trimmed = trim_bet_sequence_log(logs, runtime)
    assert len(trimmed) == 5000
    assert runtime["bet_reset_log_index"] == 5000

    logs = trimmed + [{"bet_id": "new-chain-1", "result": None, "profit": 0}]
    trimmed = trim_bet_sequence_log(logs, runtime)

    assert len(trimmed) == 5000
    assert runtime["bet_reset_log_index"] == 4999
    assert trimmed[-1]["bet_id"] == "new-chain-1"


def test_process_bet_on_sends_heal_notice_when_stale_pending_entries_are_fixed(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5094"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "自愈提示用户"},
            "telegram": {"user_id": 5094},
            "groups": {"admin_chat": 5094},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = True
    rt["stop_count"] = 2
    rt["initial_amount"] = 50_000
    rt["bet_sequence_count"] = 6
    rt["lose_count"] = 5
    rt["bet_amount"] = 2_145_500
    rt["lose_once"] = 2.1
    rt["lose_twice"] = 2.1
    rt["lose_three"] = 2.1
    rt["lose_four"] = 2.05
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_25", "amount": 106_000, "result": "赢", "profit": 104_940},
        {"bet_id": "20260312_1_26", "amount": 50_000, "result": "输", "profit": -50_000},
        {"bet_id": "20260312_1_27", "amount": 106_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_28", "amount": 225_000, "result": "输", "profit": -225_000},
        {"bet_id": "20260312_1_29", "amount": 477_000, "result": None, "profit": 0},
    ]

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5094, id=len(sent_messages))

    async def fake_refresh_pause_countdown_notice(client, user_ctx, global_cfg, remaining_rounds):
        return None

    async def fake_clear_pause_countdown_notice(client, user_ctx):
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "_refresh_pause_countdown_notice", fake_refresh_pause_countdown_notice)
    monkeypatch.setattr(zm, "_clear_pause_countdown_notice", fake_clear_pause_countdown_notice)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(
        id=9902,
        chat_id=5094,
        reply_markup=None,
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(DummyClient(), event, ctx, {}))

    assert any("已修正历史异常挂单" in message for message in sent_messages)
    assert any("当前连续押注：2 次" in message for message in sent_messages)
    assert any("下一手预计下注：47.70 万" in message for message in sent_messages)
    assert rt["stop_count"] == 1
    assert rt["bet_sequence_count"] == 2
    assert rt["lose_count"] == 2


def test_process_bet_on_skips_duplicate_trigger_when_previous_bet_pending(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5092"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "重复下注保护用户"},
            "telegram": {"user_id": 5092},
            "groups": {"admin_chat": 5092},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = True
    rt["bet_on"] = True
    ctx.state.history = [0, 1] * 30
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_28", "sequence": 2, "direction": "small", "amount": 225_000, "result": None, "profit": 0}
    ]

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5092, id=1)

    async def fake_transient_notice(client, user_ctx, global_cfg, message, **kwargs):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5092, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "_send_transient_admin_notice", fake_transient_notice)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(
        id=9901,
        chat_id=5092,
        reply_markup=None,
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(DummyClient(), event, ctx, {}))

    assert len(ctx.state.bet_sequence_log) == 1
    assert ctx.state.bet_sequence_log[0]["bet_id"] == "20260312_1_28"
    assert ctx.state.bet_sequence_log[0]["result"] is None
    assert rt["bet"] is True
    assert any("上一手仍待结算" in message for message in sent_messages)


def test_process_bet_on_runtime_heals_pending_bet_when_history_has_advanced(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "runtime_heal_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "运行中自愈用户"},
            "telegram": {"user_id": 5096},
            "groups": {"admin_chat": 5096},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["bet_amount"] = 500
    rt["bet_type"] = 0
    rt["lose_count"] = 0
    rt["win_count"] = 0
    ctx.state.history = [0, 1] * 20
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260322_1_100", "sequence": 1, "direction": "small", "amount": 500, "result": None, "profit": 0}
    ]

    sent_messages = []
    log_events = []

    async def fake_predict(user_ctx, global_cfg):
        user_ctx.state.runtime["last_predict_info"] = "runtime-heal"
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5096, id=len(sent_messages))

    async def fake_transient_notice(client, user_ctx, global_cfg, message, **kwargs):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5096, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    def fake_log_event(level, module, event, message=None, **kwargs):
        log_events.append((module, event, kwargs))

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "_send_transient_admin_notice", fake_transient_notice)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(zm, "log_event", fake_log_event)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["1", "0"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 5096
            self.id = 101
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert ctx.state.bet_sequence_log[0]["result"] == "输"
    assert ctx.state.bet_sequence_log[-1]["result"] is None
    assert any("运行中已按历史补结算" in message for message in sent_messages)
    assert any("押注执行" in message for message in sent_messages)
    assert any(event == "运行中按历史推断补结算" for _, event, _ in log_events)
    assert any(event == "下注执行完成" for _, event, _ in log_events)


def test_process_bet_on_inferred_settle_keeps_martingale_step(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "runtime_chain_keep_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "倍投保持用户"},
            "telegram": {"user_id": 5099},
            "groups": {"admin_chat": 5099},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["bet_type"] = 0
    rt["bet_amount"] = 1_461_000
    rt["lose_count"] = 3
    rt["win_count"] = 0
    rt["lose_stop"] = 9
    rt["lose_four"] = 2.05
    ctx.state.history = [0, 1] * 20
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260323_2_263", "sequence": 4, "direction": "small", "amount": 1_461_000, "result": None, "profit": 0}
    ]

    async def fake_predict(user_ctx, global_cfg):
        return 1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5099, id=1)

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self):
            history = " ".join((["1", "0"] * 20))
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history}")
            self.reply_markup = object()
            self.chat_id = 5099
            self.id = 102
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    event = DummyEvent()
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert ctx.state.bet_sequence_log[0]["result"] == "输"
    assert rt["lose_count"] == 4
    assert rt["bet_amount"] == 3_025_000


def test_process_bet_on_pause_countdown_completion_restores_flag(tmp_path):
    user_dir = tmp_path / "users" / "pause_flag_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "暂停恢复用户"},
            "telegram": {"user_id": 5098},
            "groups": {"admin_chat": 5098},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = False
    rt["mode_stop"] = False
    rt["stop_count"] = 1
    rt["flag"] = False
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = "盈利达成暂停"
    rt["pause_countdown_total_rounds"] = 2
    rt["pause_countdown_last_remaining"] = 1

    event = SimpleNamespace(
        id=60001,
        chat_id=5098,
        reply_markup=None,
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["stop_count"] == 0
    assert rt["flag"] is True
    assert rt["bet_on"] is True
    assert rt["pause_countdown_active"] is False
    assert rt["pause_countdown_reason"] == ""
    assert rt["pause_countdown_last_remaining"] == -1


def test_process_bet_on_updates_pause_countdown_remaining(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "pause_refresh_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "暂停刷新用户"},
            "telegram": {"user_id": 5099},
            "groups": {"admin_chat": 5099},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = False
    rt["mode_stop"] = False
    rt["stop_count"] = 2
    rt["flag"] = False
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = "盈利达成暂停"
    rt["pause_countdown_total_rounds"] = 2
    rt["pause_countdown_last_remaining"] = 2

    refreshed = {}

    async def fake_refresh_pause_countdown_notice(client, user_ctx, global_cfg, remaining_rounds):
        refreshed["remaining_rounds"] = remaining_rounds

    monkeypatch.setattr(zm, "_refresh_pause_countdown_notice", fake_refresh_pause_countdown_notice)

    event = SimpleNamespace(
        id=60002,
        chat_id=5099,
        reply_markup=None,
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["stop_count"] == 1
    assert rt["pause_countdown_last_remaining"] == 1
    assert refreshed["remaining_rounds"] == 1


def test_process_bet_on_force_unlocks_after_repeated_skip(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "skip_unlock_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "观望解锁用户"},
            "telegram": {"user_id": 5100},
            "groups": {"admin_chat": 5100},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["lose_count"] = 4
    rt["bet_amount"] = 1_461_000
    rt["bet_type"] = 0
    ctx.state.history = [0, 1] * 20

    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        return -1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5100, id=len(sent_messages))

    async def fake_sleep(*args, **kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    class DummyEvent:
        def __init__(self, history_text, event_id):
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history_text}")
            self.reply_markup = object()
            self.chat_id = 5100
            self.id = event_id
            self.clicks = []

        async def click(self, data):
            self.clicks.append(data)

    events = [
        DummyEvent(
            " ".join(
                ["1", "1", "0", "1", "0", "1"]
                + (["0", "1"] * 11)
                + ["0", "0", "0", "1", "0", "1", "0", "1", "0", "1", "0", "1"]
            ),
            1,
        ),
        DummyEvent(
            " ".join(
                ["0", "0", "1", "0", "1", "0"]
                + (["1", "0"] * 11)
                + ["1", "1", "1", "0", "1", "0", "1", "0", "1", "0", "1", "0"]
            ),
            2,
        ),
        DummyEvent(
            " ".join(
                ["1", "0", "0", "1", "0", "1"]
                + (["1", "0"] * 11)
                + ["1", "0", "0", "1", "0", "1", "1", "0", "1", "0", "1", "0"]
            ),
            3,
        ),
    ]

    asyncio.run(zm.process_bet_on(SimpleNamespace(), events[0], ctx, {}))
    assert rt["bet"] is False

    asyncio.run(zm.process_bet_on(SimpleNamespace(), events[1], ctx, {}))
    assert rt["bet"] is False

    asyncio.run(zm.process_bet_on(SimpleNamespace(), events[2], ctx, {}))
    assert rt["bet"] is True
    assert any("低手位连续观望，已保守解锁" in message for message in sent_messages)


def test_process_bet_on_high_step_skip_pauses_instead_of_unlock(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "skip_pause_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "高手位观望用户"},
            "telegram": {"user_id": 5101},
            "groups": {"admin_chat": 5101},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["bet_sequence_count"] = 3
    rt["bet_amount"] = 328_500
    ctx.state.history = [0, 1] * 20

    sent_messages = []

    async def fake_predict(user_ctx, global_cfg):
        return -1

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5101, id=len(sent_messages))

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    class DummyEvent:
        def __init__(self, history_text, event_id):
            self.message = SimpleNamespace(message=f"[近 40 次结果][由近及远][0 小 1 大] {history_text}")
            self.reply_markup = object()
            self.chat_id = 5101
            self.id = event_id

    events = [
        DummyEvent(
            " ".join(
                ["1", "1", "0", "1", "0", "1"]
                + (["0", "1"] * 11)
                + ["0", "0", "0", "1", "0", "1", "0", "1", "0", "1", "0", "1"]
            ),
            1,
        ),
        DummyEvent(
            " ".join(
                ["0", "0", "1", "0", "1", "0"]
                + (["1", "0"] * 11)
                + ["1", "1", "1", "0", "1", "0", "1", "0", "1", "0", "1", "0"]
            ),
            2,
        ),
    ]

    asyncio.run(zm.process_bet_on(SimpleNamespace(), events[0], ctx, {}))
    assert rt["bet"] is False
    assert rt["stop_count"] == 0

    asyncio.run(zm.process_bet_on(SimpleNamespace(), events[1], ctx, {}))
    assert rt["bet"] is False
    assert rt["bet_on"] is False
    assert rt["stop_count"] > 0
    assert rt["pause_countdown_reason"] == "高手位连续观望暂停"
    assert any("高手位连续观望，已自动暂停" in message for message in sent_messages)


def test_process_bet_on_click_timeout_does_not_advance_bet_amount(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "click_timeout_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "点击超时用户"},
            "telegram": {"user_id": 5102},
            "groups": {"admin_chat": 5102},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["lose_count"] = 5
    rt["bet_sequence_count"] = 5
    rt["bet_amount"] = 909000
    rt["current_round"] = 5
    rt["current_bet_seq"] = 15
    ctx.state.history = [0, 1] * 20

    sent = []

    async def fake_predict(user_ctx, global_cfg):
        return 1

    async def fake_click_recover(client, event, user_ctx, button_data):
        raise asyncio.TimeoutError()

    async def fake_transient_notice(client, user_ctx, global_cfg, message, ttl_seconds=120, attr_name="x", msg_type="info"):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=5102, id=len(sent))

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "_click_bet_button_with_recover", fake_click_recover)
    monkeypatch.setattr(zm, "_send_transient_admin_notice", fake_transient_notice)

    event = SimpleNamespace(
        id=99010,
        chat_id=5102,
        reply_markup=SimpleNamespace(rows=[]),
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["bet"] is False
    assert rt["bet_sequence_count"] == 5
    assert rt["bet_amount"] == 909000
    assert any("⏰ 本轮下注响应超时" in message for _, message in sent)
    assert any("目标金额：" in message and "按钮数量：" in message for _, message in sent)


def test_process_bet_on_stale_window_reports_window_expired(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "stale_window_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "窗口失效用户"},
            "telegram": {"user_id": 5103},
            "groups": {"admin_chat": 5103},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["lose_count"] = 5
    rt["bet_sequence_count"] = 5
    rt["bet_amount"] = 909000
    ctx.state.history = [0, 1] * 20

    sent = []

    async def fake_predict(user_ctx, global_cfg):
        return 1

    async def fake_click_recover(client, event, user_ctx, button_data):
        raise RuntimeError("下注窗口失效且未找到可用的最新下注消息")

    async def fake_transient_notice(client, user_ctx, global_cfg, message, ttl_seconds=120, attr_name="x", msg_type="info"):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=5103, id=len(sent))

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "_click_bet_button_with_recover", fake_click_recover)
    monkeypatch.setattr(zm, "_send_transient_admin_notice", fake_transient_notice)

    event = SimpleNamespace(
        id=99011,
        chat_id=5103,
        reply_markup=SimpleNamespace(rows=[]),
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["bet"] is False
    assert rt["bet_amount"] == 909000
    assert any("⏰ 本轮下注窗口已失效" in message for _, message in sent)


def test_process_settle_warn_message_uses_real_settled_chain_count(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5093"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "连押纠偏用户"},
            "telegram": {"user_id": 5093},
            "groups": {"admin_chat": 5093},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小，下面开大 -> 输
    rt["bet_amount"] = 2_145_500
    rt["bet_sequence_count"] = 6
    rt["lose_count"] = 2
    rt["warning_lose_count"] = 3
    rt["current_round"] = 1
    rt["current_bet_seq"] = 32
    rt["current_preset_name"] = "yc50000"
    rt["account_balance"] = 14_508_552
    rt["gambling_fund"] = 6_187_734
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260312_1_25", "amount": 106_000, "result": "赢", "profit": 104_940},
        {"bet_id": "20260312_1_26", "amount": 50_000, "result": "输", "profit": -50_000},
        {"bet_id": "20260312_1_27", "amount": 106_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_28", "amount": 225_000, "result": "输", "profit": -225_000},
        {"bet_id": "20260312_1_29", "amount": 477_000, "result": None, "profit": 0},
        {"bet_id": "20260312_1_30", "amount": 1_011_500, "result": None, "profit": 0},
        {"bet_id": "20260312_1_31", "amount": 2_145_500, "result": None, "profit": 0},
    ]

    captured = {}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        if msg_type == "lose_streak":
            captured["message"] = message
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5093, id=1)

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=45031, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    msg = captured["message"]
    assert "⚠️ 3 连输告警" in msg
    assert "当前链路已进入高关注状态" in msg
    assert "连续押注：3 次" in msg
    assert "累计损失：242.05 万" in msg


def test_process_settle_writes_chain_diagnostic_logs(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "settle_diag_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算诊断用户"},
            "telegram": {"user_id": 5097},
            "groups": {"admin_chat": 5097},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1
    rt["bet_amount"] = 1000
    rt["bet_sequence_count"] = 2
    rt["lose_count"] = 1
    ctx.state.bet_sequence_log = [{"bet_id": "20260323_2_260", "amount": 1000, "result": None, "profit": 0}]

    log_events = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=5097, id=1)

    async def fake_fetch_balance(user_ctx):
        return 100000

    def fake_log_event(level, module, event, message=None, **kwargs):
        log_events.append((module, event, kwargs))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)
    monkeypatch.setattr(zm, "log_event", fake_log_event)
    monkeypatch.setenv("YDXBOT_VERBOSE_RUNTIME_LOGS", "1")

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=55001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert any(event == "收到结算并开始回写" for _, event, _ in log_events)
    assert any(event == "结算前链路诊断" for _, event, _ in log_events)
    assert any(event == "结算后链路回写完成" for _, event, _ in log_events)
    assert rt["last_settle_message_id"] == 55001
    assert ctx.state.bet_sequence_log[0]["result"] == "赢"


def test_process_bet_on_insufficient_fund_sends_pause_notice_even_without_pending_bet(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5019"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "资金不足用户"},
            "telegram": {"user_id": 5019},
            "groups": {"admin_chat": 5019},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = False
    rt["stop_count"] = 0
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["initial_amount"] = 500
    rt["lose_count"] = 0
    rt["gambling_fund"] = 100

    sent_messages = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        if msg_type == "fund_pause":
            sent_messages.append(message)
        return None

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)

    event = SimpleNamespace(reply_markup=object(), message=SimpleNamespace(message="unused"))
    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert any("资金不足，已暂停押注" in m for m in sent_messages)
    assert any("恢复方式：`gf [金额]`" in m for m in sent_messages)
    assert rt["fund_pause_notified"] is True
    assert rt["bet"] is False
    assert rt["bet_on"] is False


def test_check_bet_status_does_not_resume_when_next_bet_amount_is_zero(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5020"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "上限暂停用户"},
            "telegram": {"user_id": 5020},
            "groups": {"admin_chat": 5020},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["manual_pause"] = False
    rt["bet"] = False
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["initial_amount"] = 10000
    rt["bet_amount"] = 10000
    rt["lose_stop"] = 3
    rt["lose_count"] = 3  # 下一手将超过上限，calculate_bet_amount 返回0
    rt["gambling_fund"] = 10_000_000

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5020, id=len(sent_messages))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    asyncio.run(zm.check_bet_status(SimpleNamespace(), ctx, {}))

    assert any("已达到预设连投上限" in m for m in sent_messages)
    assert any("当前上限：3 手" in m for m in sent_messages)
    assert rt["limit_stop_notified"] is True
    assert rt["bet"] is False
    assert rt["bet_on"] is False
    assert not any("押注已恢复" in m for m in sent_messages)


def test_process_settle_keeps_gambling_fund_independent_before_next_bet_check(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5021"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算资金不足用户"},
            "telegram": {"user_id": 5021},
            "groups": {"admin_chat": 5021},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小，下面开大 -> 输
    rt["bet_amount"] = 730000
    rt["lose_count"] = 4
    rt["lose_stop"] = 9
    rt["lose_four"] = 2.05
    rt["current_round"] = 1
    rt["current_bet_seq"] = 9
    rt["account_balance"] = 2_200_000
    rt["gambling_fund"] = 1_417_800
    rt["fund_pause_notified"] = True
    ctx.state.bet_sequence_log = [{"bet_id": "20260228_1_9", "profit": None}]

    sent_messages = []

    async def fake_send_message_v2(*args, **kwargs):
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5021, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=44001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert rt["gambling_fund"] == 687_800
    assert rt["fund_pause_notified"] is True
    assert rt["bet_on"] is False
    assert rt["mode_stop"] is True
    assert not any("菠菜资金不足，已暂停押注" in m for m in sent_messages)


def test_process_settle_keeps_pending_bet_settlement_before_fund_pause(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5022"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "结算时序用户"},
            "telegram": {"user_id": 5022},
            "groups": {"admin_chat": 5022},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 0  # 押小，开大 -> 输
    rt["bet_amount"] = 1_322_000
    rt["bet_sequence_count"] = 8
    rt["lose_count"] = 7
    rt["lose_stop"] = 12
    rt["lose_four"] = 2.05
    rt["current_round"] = 1
    rt["current_bet_seq"] = 90
    rt["account_balance"] = 1_334_559
    rt["gambling_fund"] = 1_334_559
    ctx.state.bet_sequence_log = [{"bet_id": "20260302_1_90", "result": None, "profit": 0}]

    sent_messages = []
    sent_types = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", title=None, desp=None):
        sent_types.append(msg_type)
        sent_messages.append(message)
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5022, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        # 模拟远端余额已变化（比如该笔下注已在平台侧扣减）
        return 12_559

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event = SimpleNamespace(id=44002, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event, ctx, {}))

    assert ctx.state.bet_sequence_log[-1]["result"] == "输"
    assert ctx.state.bet_sequence_log[-1]["profit"] == -1_322_000
    assert rt["gambling_fund"] == 12_559
    assert rt["bet"] is False
    assert "fund_pause" in sent_types
    assert any("资金不足，已暂停押注" in m for m in sent_messages)


def test_process_settle_only_consumes_pending_bet_once(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5015"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "单次结算用户"},
            "telegram": {"user_id": 5015},
            "groups": {"admin_chat": 5015},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet"] = True
    rt["bet_type"] = 1
    rt["bet_amount"] = 1_000
    rt["current_round"] = 1
    rt["current_bet_seq"] = 1
    rt["account_balance"] = 10_000_000
    rt["gambling_fund"] = 9_000_000
    ctx.state.bet_sequence_log = [{"bet_id": "20260227_1_1", "profit": None}]

    sent_messages = []

    async def fake_send_message_v2(*args, **kwargs):
        return None

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5015, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return rt["account_balance"]

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    class DummyClient:
        async def send_message(self, target, message, parse_mode=None):
            return SimpleNamespace(chat_id=target, id=1)

        async def delete_messages(self, chat_id, message_id):
            return None

    event1 = SimpleNamespace(id=42001, message=SimpleNamespace(message="已结算: 结果为 9 大"))
    asyncio.run(zm.process_settle(DummyClient(), event1, ctx, {}))
    first_result_msgs = [m for m in sent_messages if "押注结果" in m]
    assert len(first_result_msgs) == 1
    assert rt["bet"] is False

    sent_messages.clear()
    event2 = SimpleNamespace(id=42002, message=SimpleNamespace(message="已结算: 结果为 8 小"))
    asyncio.run(zm.process_settle(DummyClient(), event2, ctx, {}))
    second_result_msgs = [m for m in sent_messages if "押注结果" in m]
    assert len(second_result_msgs) == 0


def test_process_user_command_explain_returns_last_logic_audit(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "explain_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "解释用户"},
            "telegram": {"user_id": 70151},
            "groups": {"admin_chat": 70151},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.runtime["last_logic_audit"] = json.dumps({"prediction": 1, "tag": "TEST"}, ensure_ascii=False)

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70151, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="explain", chat_id=70151, id=8)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert sent_messages
    assert "最近一次模型判断依据" in sent_messages[-1]
    assert '"prediction": 1' in sent_messages[-1]


def test_process_user_command_balance_uses_ops_card(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "balance_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "余额用户"},
            "telegram": {"user_id": 70152},
            "groups": {"admin_chat": 70152},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.runtime["gambling_fund"] = 456000

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70152, id=len(sent_messages))

    async def fake_fetch_balance(user_ctx):
        return 1234000

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "fetch_balance", fake_fetch_balance)

    event = SimpleNamespace(raw_text="balance", chat_id=70152, id=9)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert sent_messages
    assert "账户余额查询成功" in sent_messages[-1]
    assert "账户余额：123.40 万" in sent_messages[-1]
    assert "菠菜资金：45.60 万" in sent_messages[-1]


def test_process_user_command_stf_sets_profit_target_in_wan(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "stf_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "目标用户"},
            "telegram": {"user_id": 70160},
            "groups": {"admin_chat": 70160},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70160, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="stf 100", chat_id=70160, id=17)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert ctx.state.runtime["profit"] == 1_000_000
    assert sent_messages
    assert "✅ 本轮目标金额已更新" in sent_messages[-1]
    assert "当前目标：100.00 万" in sent_messages[-1]


def test_process_user_command_users_uses_ops_card(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "users_info_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "用户信息用户"},
            "telegram": {"user_id": 70153},
            "groups": {"admin_chat": 70153},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["gambling_fund"] = 888000
    rt["current_preset_name"] = "yc05"
    rt["current_model_id"] = "model-x"
    rt["win_total"] = 12
    rt["total"] = 20

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70153, id=len(sent_messages))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    event = SimpleNamespace(raw_text="users", chat_id=70153, id=10)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert sent_messages
    assert "当前用户信息" in sent_messages[-1]
    assert "账号：用户信息用户 (ID: 70153)" in sent_messages[-1]
    assert "模型：model-x" in sent_messages[-1]
    assert "胜率：12/20" in sent_messages[-1]


def test_process_user_command_model_select_uses_send_to_admin_without_event_reply(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "model_cmd_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型命令用户"},
            "telegram": {"user_id": 70161},
            "groups": {"admin_chat": 70161},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "models": {
                    "1": {"model_id": "model-a", "enabled": True},
                    "2": {"model_id": "model-b", "enabled": True},
                },
            },
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.runtime["current_model_id"] = "model-a"
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70161, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="model select 2", chat_id=70161, id=18)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert ctx.state.runtime["current_model_id"] == "model-b"
    assert any("正在切换模型" in msg for msg in sent_messages)
    assert any("模型切换成功" in msg for msg in sent_messages)


def test_process_user_command_apikey_show_uses_send_to_admin_without_event_reply(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "apikey_cmd_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "密钥命令用户"},
            "telegram": {"user_id": 70162},
            "groups": {"admin_chat": 70162},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["sk-test-abcdefghijklmnopqrstuvwxyz"],
            },
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70162, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="apikey show", chat_id=70162, id=19)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert sent_messages
    assert "当前账号 AI key 列表" in sent_messages[-1]
    assert "***" in sent_messages[-1] or "..." in sent_messages[-1]


def test_process_user_command_yss_uses_plain_preset_list_layout(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "preset_list_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "预设列表用户"},
            "telegram": {"user_id": 70158},
            "groups": {"admin_chat": 70158},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.presets = {
        "5k": ["1", "12", "3.0", "2.5", "2.2", "2.1", "5000"],
        "10w": ["1", "8", "3.0", "2.5", "2.2", "2.1", "100000"],
    }

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70158, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="yss", chat_id=70158, id=15)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert sent_messages
    msg = sent_messages[-1]
    assert msg.startswith("📚 当前预设列表\n\n")
    assert "'5k '" in msg or "'5k':" in msg
    assert "'10w':" in msg
    assert "以下是当前账号可用的全部预设" not in msg
    assert "删除可执行" not in msg


def test_process_user_command_stats_uses_html_preformatted_report(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "stats_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "统计用户"},
            "telegram": {"user_id": 70159},
            "groups": {"admin_chat": 70159},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.history = [1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 1, 0]
    ctx.state.bet_sequence_log = [
        {"profit": -1000, "result": "输"},
        {"profit": -2000, "result": "输"},
        {"profit": None, "result": None},
        {"profit": 990, "result": "赢"},
        {"profit": -3000, "result": "输"},
    ]

    sent = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", *args, **kwargs):
        sent.append((msg_type, message, parse_mode))
        return SimpleNamespace(chat_id=70159, id=len(sent))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="stats", chat_id=70159, id=16)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert sent
    msg_type, message, parse_mode = sent[-1]
    assert msg_type == "info"
    assert parse_mode == "html"
    assert message.startswith("📊 统计概览")
    assert "<pre>" in message
    assert "类别 | 1000 |  500 |  200 |  100 |" in message


def test_count_lose_streaks_ignores_unsettled_entries():
    logs = [
        {"profit": -1000, "result": "输"},
        {"profit": None, "result": None},
        {"profit": -2000, "result": "输"},
        {"profit": 990, "result": "赢"},
        {"profit": -3000, "result": "输"},
    ]

    lose_streaks = zm.count_lose_streaks(logs)

    assert lose_streaks == {2: 1, 1: 1}


def test_build_stats_report_uses_actual_window_labels_and_resolved_chain():
    state = SimpleNamespace(
        history=[1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 1, 0],
        runtime={},
        bet_sequence_log=[
            {"profit": -1000, "result": "输"},
            {"profit": -2000, "result": "输"},
            {"profit": None, "result": None},
            {"profit": 990, "result": "赢"},
            {"profit": -3000, "result": "输"},
        ],
    )

    report = zm._build_stats_report(state, windows=[1000, 5])

    assert "最近局数“连大、连小、连输”统计" in report
    assert "类别 | 1000 |    5 |" in report or "类别 | 1000 |   5 |" in report
    assert "连输" in report
    assert " --  |  -   |  -   |" in report
    assert " 0 " not in report


def test_build_stats_report_uses_account_level_resolved_bet_logs_not_current_chain():
    state = SimpleNamespace(
        history=[1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 1, 0],
        runtime={"bet_reset_log_index": 4},
        bet_sequence_log=[
            {"profit": -1000, "result": "输"},
            {"profit": -2000, "result": "输"},
            {"profit": 990, "result": "赢"},
            {"profit": -3000, "result": "输"},
            {"profit": -4000, "result": "输"},
            {"profit": 1980, "result": "赢"},
        ],
    )

    report = zm._build_stats_report(state, windows=[1000, 5])

    assert "押注统计" in report
    assert "连输" in report
    assert " 2  " in report
    assert " 1  " in report


def test_process_user_command_help_uses_quick_start_layout(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "help_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "帮助用户"},
            "telegram": {"user_id": 70154},
            "groups": {"admin_chat": 70154},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", *args, **kwargs):
        sent_messages.append((msg_type, message, parse_mode))
        return SimpleNamespace(chat_id=70154, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="help", chat_id=70154, id=11)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    msg_type, message, parse_mode = sent_messages[-1]
    assert msg_type == "info"
    assert parse_mode == "html"
    assert "<b>📘 脚本命令指南</b>" in message
    assert "⚡ 基础控制（最常用）" in message
    assert "<code>/st [预设名]</code>" in message
    assert "<code>/stats</code> 查看连大、连小、连输统计" in message
    assert "<code>/stf [数字]</code>" in message
    assert "<code>/mfb [on/off]</code>" in message
    assert "<code>/model select [编号/ID]</code>" in message
    assert "<code>/res state</code>" in message
    assert "<code>/users</code> 查看当前用户信息" in message


def test_process_user_command_mfb_off_persists_ai_setting(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "mfb_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型兜底用户"},
            "telegram": {"user_id": 70163},
            "groups": {"admin_chat": 70163},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "enable_stat_fallback_bet": True,
            },
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70163, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="mfb off", chat_id=70163, id=21)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert ctx.state.runtime["stat_fallback_bet_enabled"] is False
    assert "✅ 模型兜底开关已关闭" in sent_messages[-1]
    assert "等待模型恢复后继续" in sent_messages[-1]


def test_process_user_command_mfb_show_reports_current_mode(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "mfb_show_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型兜底查看用户"},
            "telegram": {"user_id": 70164},
            "groups": {"admin_chat": 70164},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {"enabled": True, "api_keys": ["k1"]},
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.runtime["stat_fallback_bet_enabled"] = False
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70164, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="mfb", chat_id=70164, id=22)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert "📌 模型兜底开关" in sent_messages[-1]
    assert "当前状态：关闭" in sent_messages[-1]


def test_process_user_command_update_uses_release_card_fields(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "update_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "更新用户"},
            "telegram": {"user_id": 70155},
            "groups": {"admin_chat": 70155},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70155, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(
        zm,
        "update_to_version",
        lambda repo_root=None, target_ref=None: {
            "success": True,
            "resolved_target": target_ref or "v1.2.0",
            "after": {"display_version": "v1.2.0"},
        },
    )
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="update v1.2.0", chat_id=70155, id=12)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert "🔄 开始更新" in sent_messages[0]
    assert "目标版本：v1.2.0" in sent_messages[0]
    success_message = sent_messages[-1]
    assert "✅ 更新成功" in success_message
    assert "目标版本：v1.2.0" in success_message
    assert "当前版本：v1.2.0" in success_message
    assert "重启命令：`restart`" in success_message


def test_process_user_command_reback_uses_release_card_fields(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "reback_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "回退用户"},
            "telegram": {"user_id": 70156},
            "groups": {"admin_chat": 70156},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70156, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(
        zm,
        "reback_to_version",
        lambda repo_root=None, target_ref=None: {
            "success": True,
            "resolved_target": target_ref or "v1.1.0",
            "after": {"display_version": "v1.1.0"},
        },
    )
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="reback v1.1.0", chat_id=70156, id=13)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    assert "↩️ 开始回退" in sent_messages[0]
    success_message = sent_messages[-1]
    assert "✅ 回退成功" in success_message
    assert "目标版本：v1.1.0" in success_message
    assert "当前版本：v1.1.0" in success_message
    assert "重启命令：`restart`" in success_message


def test_process_user_command_restart_uses_release_card_fields(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "restart_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "重启用户"},
            "telegram": {"user_id": 70157},
            "groups": {"admin_chat": 70157},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=70157, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    async def fake_restart_process():
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm, "resolve_systemd_service_name", lambda: "ydxbot.service")
    monkeypatch.setattr(zm, "restart_process", fake_restart_process)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(raw_text="restart", chat_id=70157, id=14)
    asyncio.run(zm.process_user_command(SimpleNamespace(), event, ctx, {}))

    message = sent_messages[-1]
    assert "♻️ 开始重启" in message
    assert "服务名：ydxbot.service" in message
    assert "重启方式：" not in message
    assert "是否需要等待：" not in message


def test_format_dashboard_shows_software_version_and_preset_lines(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5013"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "仪表盘用户"},
            "telegram": {"user_id": 5013},
            "groups": {"admin_chat": 5013},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["current_preset_name"] = "yc10"
    rt["continuous"] = 1
    rt["lose_stop"] = 11
    rt["lose_once"] = 2.8
    rt["lose_twice"] = 2.3
    rt["lose_three"] = 2.2
    rt["lose_four"] = 2.05
    rt["initial_amount"] = 10000
    ctx.state.history = [1, 0] * 20

    monkeypatch.setattr(zm, "get_current_repo_info", lambda: {"current_tag": "v1.0.10", "nearest_tag": "v1.0.10", "short_commit": "abcd1234"})

    msg = zm.format_dashboard(ctx)
    assert "<b>版本：</b>v1.0.10(abcd1234)" in msg
    assert "<b>方案：</b> yc10" in msg
    assert "├ 账户余额：" in msg
    assert "├ 菠菜资金：" in msg
    assert "<b>原始参数：</b> 1 11 2.8 2.3 2.2 2.05 10000" in msg


def test_st_command_triggers_auto_yc_report(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5008"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "预设测算用户"},
            "telegram": {"user_id": 5008},
            "groups": {"admin_chat": 5008},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))

    sent_messages = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5008, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="st 5k", chat_id=5008, id=21)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    assert ctx.state.runtime.get("current_preset_name") == "5k"
    assert any("预设启动成功: 5k" in msg for msg in sent_messages)
    assert any("🔮 已根据当前预设自动测算" in msg for msg in sent_messages)
    assert any("🎯 策略参数" in msg for msg in sent_messages)
    assert any("连数|倍率|下注| 盈利 |所需本金" in msg for msg in sent_messages)


def test_xx_command_cleans_messages_in_config_groups(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5009"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "清理用户"},
            "telegram": {"user_id": 5009},
            "groups": {"admin_chat": 5009, "zq_group": [111, 222]},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))

    sent_messages = []
    deleted_calls = []

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent_messages.append(message)
        return SimpleNamespace(chat_id=5009, id=len(sent_messages))

    def fake_create_task(coro):
        coro.close()
        return None

    class DummyClient:
        def iter_messages(self, chat_id, from_user=None, limit=None):
            async def _gen():
                sample = {111: [1, 2, 3], 222: [10]}
                for msg_id in sample.get(chat_id, []):
                    yield SimpleNamespace(id=msg_id)

            return _gen()

        async def delete_messages(self, chat_id, message_ids):
            deleted_calls.append((chat_id, list(message_ids)))

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="xx", chat_id=5009, id=30)
    asyncio.run(zm.process_user_command(DummyClient(), cmd_event, ctx, {}))

    assert (111, [1, 2, 3]) in deleted_calls
    assert (222, [10]) in deleted_calls
    assert any("群组消息已清理" in msg for msg in sent_messages)
    assert any("删除消息：4" in msg for msg in sent_messages)


def test_process_red_packet_claim_success_sends_admin_notice(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5010"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "红包用户"},
            "telegram": {"user_id": 5010},
            "groups": {"admin_chat": 5010, "zq_bot": 9001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent = {}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["message"] = message
        return SimpleNamespace(chat_id=5010, id=1)

    async def fake_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    class DummyClient:
        async def __call__(self, request):
            return SimpleNamespace(message="已获得 88 灵石")

    class DummyButton:
        data = b"red-packet"

    class DummyRow:
        buttons = [DummyButton()]

    class DummyMarkup:
        rows = [DummyRow()]

    class DummyEvent:
        sender_id = 9001
        raw_text = "恭喜领取灵石红包"
        text = "恭喜领取灵石红包"
        chat_id = -10001
        id = 99
        reply_markup = DummyMarkup()

        def __init__(self):
            self.clicked = []

        async def click(self, *args):
            self.clicked.append(args)

    event = DummyEvent()
    asyncio.run(zm.process_red_packet(DummyClient(), event, ctx, {}))

    assert event.clicked
    assert sent.get("message") == "🎉 抢到红包88灵石！"


def test_process_red_packet_ignores_game_message(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "5012"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "游戏过滤用户"},
            "telegram": {"user_id": 5012},
            "groups": {"admin_chat": 5012, "zq_bot": 9001},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent = {"called": False}

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        sent["called"] = True
        return SimpleNamespace(chat_id=5012, id=1)

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)

    class DummyClient:
        async def __call__(self, request):
            raise AssertionError("游戏消息不应触发红包回调请求")

    class DummyButton:
        data = b"game-start"
        text = "开始游戏"

    class DummyRow:
        buttons = [DummyButton()]

    class DummyMarkup:
        rows = [DummyRow()]

    class DummyEvent:
        sender_id = 9001
        raw_text = "灵石对战游戏开始啦"
        text = "灵石对战游戏开始啦"
        chat_id = -10001
        id = 109
        reply_markup = DummyMarkup()

        def __init__(self):
            self.clicked = []

        async def click(self, *args):
            self.clicked.append(args)

    event = DummyEvent()
    asyncio.run(zm.process_red_packet(DummyClient(), event, ctx, {}))

    assert event.clicked == []
    assert sent["called"] is False
def test_generate_mobile_bet_report_formats_bet_id():
    report = zm.generate_mobile_bet_report(
        history=[0, 0, 1, 0],
        direction="小",
        amount=5000,
        sequence_count=1,
        bet_id="20260318_3_58",
    )

    assert "3月18日第 3 轮第 58 次押注执行" in report
    assert "20260318_3_58押注执行" not in report


def test_predict_next_bet_core_updates_current_model_after_fallback(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "fallback_model_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "闄嶇骇妯″瀷鐢ㄦ埛"},
            "telegram": {"user_id": 70121},
            "groups": {"admin_chat": 70121},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            },
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.history = [0, 1] * 30
    rt = ctx.state.runtime
    rt["current_model_id"] = "model-1"

    class FakeModelManager:
        async def call_model(self, model_id, messages, **kwargs):
            assert model_id == "model-1"
            return {
                "success": True,
                "error": "",
                "content": '{"prediction": 1, "confidence": 91, "reason": "fallback"}',
                "model_id": "model-2",
                "requested_model_id": "model-1",
                "fallback_used": True,
            }

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())

    prediction = asyncio.run(zm.predict_next_bet_core(ctx, {}))

    assert prediction == 1
    assert rt["current_model_id"] == "model-2"
    assert rt["pending_model_notice"]["type"] == "switch"
    assert rt["pending_model_notice"]["from_model"] == "model-1"
    assert rt["pending_model_notice"]["to_model"] == "model-2"
    assert rt["model_health_status"] == "switched"
    assert '"model_id": "model-2"' in rt["last_logic_audit"]


def test_predict_next_bet_core_queues_failure_notice_when_model_chain_unavailable(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "fallback_fail_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型失败用户"},
            "telegram": {"user_id": 70125},
            "groups": {"admin_chat": 70125},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            },
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.history = [0, 1] * 30
    rt = ctx.state.runtime
    rt["current_model_id"] = "model-1"

    class FakeModelManager:
        async def call_model(self, model_id, messages, **kwargs):
            return {
                "success": False,
                "error": "model-1 调用失败: 超时 | model-2 调用失败: 429",
                "content": "",
            }

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())

    prediction = asyncio.run(zm.predict_next_bet_core(ctx, {}))

    assert prediction in (0, 1)
    assert rt["pending_model_notice"]["type"] == "failure"
    assert rt["pending_model_notice"]["from_model"] == "model-1"
    assert "超时" in rt["pending_model_notice"]["detail"]
    assert rt["model_fallback_streak"] >= 1


def test_predict_next_bet_core_waits_for_model_when_stat_fallback_disabled(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "fallback_wait_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型等待用户"},
            "telegram": {"user_id": 70126},
            "groups": {"admin_chat": 70126},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "enable_stat_fallback_bet": False,
                "models": {
                    "1": {"model_id": "model-1", "enabled": True},
                    "2": {"model_id": "model-2", "enabled": True},
                },
                "fallback_chain": ["1", "2"],
            },
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.history = [0, 1] * 30
    rt = ctx.state.runtime
    rt["stat_fallback_bet_enabled"] = False
    rt["current_model_id"] = "model-1"

    class FakeModelManager:
        async def call_model(self, model_id, messages, **kwargs):
            return {
                "success": False,
                "error": "model-1 调用失败: 超时 | model-2 调用失败: 429",
                "content": "",
            }

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())

    prediction = asyncio.run(zm.predict_next_bet_core(ctx, {}))

    assert prediction == -1
    assert rt["last_predict_source"] == "model_wait"
    assert "等待模型恢复后再下注" in rt["last_predict_info"]
    assert rt["model_fallback_streak"] >= 1


def test_process_bet_on_timeout_fallback_pauses_after_threshold(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "model_pause_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型暂停用户"},
            "telegram": {"user_id": 70200},
            "groups": {"admin_chat": 70200},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["manual_pause"] = False
    rt["initial_amount"] = 50000
    rt["bet_amount"] = 50000
    rt["lose_count"] = 0
    rt["model_fallback_streak"] = 4
    rt["current_model_id"] = "qwen/qwen3-next-80b-a3b-instruct"
    ctx.state.history = [0, 1] * 20

    sent = []

    async def fake_predict(user_ctx, global_cfg):
        raise asyncio.TimeoutError()

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", *args, **kwargs):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=70200, id=len(sent))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(
        id=99001,
        chat_id=70200,
        reply_markup=SimpleNamespace(rows=[]),
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["model_fallback_streak"] >= 5
    assert rt["model_health_status"] == "down"
    assert rt["stop_count"] > 0
    assert any(msg_type == "model_pause" for msg_type, _ in sent)


def test_process_bet_on_timeout_waits_for_model_when_stat_fallback_disabled(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "model_wait_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型等待下注用户"},
            "telegram": {"user_id": 70202},
            "groups": {"admin_chat": 70202},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {"enable_stat_fallback_bet": False},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["stat_fallback_bet_enabled"] = False
    rt["switch"] = True
    rt["bet_on"] = True
    rt["mode_stop"] = True
    rt["manual_pause"] = False
    rt["initial_amount"] = 50000
    rt["bet_amount"] = 50000
    rt["lose_count"] = 0
    rt["current_model_id"] = "model-x"
    ctx.state.history = [0, 1] * 20

    sent = []

    async def fake_predict(user_ctx, global_cfg):
        raise asyncio.TimeoutError()

    async def fake_notice(client, user_ctx, global_cfg, message, ttl_seconds=120, attr_name="x", msg_type="info"):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=70202, id=len(sent))

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "predict_next_bet_core", fake_predict)
    monkeypatch.setattr(zm, "_send_transient_admin_notice", fake_notice)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    event = SimpleNamespace(
        id=99002,
        chat_id=70202,
        reply_markup=SimpleNamespace(rows=[]),
        message=SimpleNamespace(message="[0 小 1 大] 0 1 0 1 0 1"),
    )

    asyncio.run(zm.process_bet_on(SimpleNamespace(), event, ctx, {}))

    assert rt["bet"] is False
    assert rt["bet_on"] is True
    assert rt["last_predict_source"] == "timeout_wait"
    assert rt["stall_guard_skip_streak"] == 0
    assert any("⏸️ 本局等待模型恢复" in message for _, message in sent)


def test_model_probe_loop_auto_resumes_after_recovery(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "model_probe_resume_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型恢复用户"},
            "telegram": {"user_id": 70201},
            "groups": {"admin_chat": 70201},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["current_model_id"] = "model-1"
    rt["model_health_status"] = "down"
    rt["model_fallback_streak"] = 5
    rt["model_pause_active"] = True
    rt["bet_on"] = False
    rt["mode_stop"] = True
    rt["switch"] = True
    rt["stop_count"] = 2
    rt["pause_countdown_active"] = True
    rt["pause_countdown_reason"] = "模型连续兜底暂停"
    rt["pause_countdown_total_rounds"] = 2
    rt["model_probe_index"] = 1

    sent = []

    class FakeModelManager:
        fallback_chain = ["1", "2"]
        models = [
            {"model_id": "model-1", "provider": "iflow", "enabled": True},
            {"model_id": "model-2", "provider": "iflow", "enabled": True},
        ]

        def get_model(self, key):
            mapping = {
                "1": self.models[0],
                "2": self.models[1],
                "model-1": self.models[0],
                "model-2": self.models[1],
            }
            return mapping.get(str(key))

        async def _call_iflow(self, model_cfg, messages, **kwargs):
            return {"success": True, "content": "OK"}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", *args, **kwargs):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=70201, id=len(sent))

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())
    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)

    asyncio.run(zm._run_model_probe_loop(SimpleNamespace(), ctx, {}))

    assert rt["current_model_id"] == "model-2"
    assert rt["model_pause_active"] is False
    assert rt["model_fallback_streak"] == 0
    assert rt["bet_on"] is True
    assert rt["stop_count"] == 0
    assert rt["pause_countdown_active"] is False
    assert rt["model_probe_active"] is False
    assert any(msg_type == "model_resume" for msg_type, _ in sent)


def test_model_probe_loop_notifies_priority_only_after_full_cycle_failure(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "model_probe_fail_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "模型探测失败用户"},
            "telegram": {"user_id": 70202},
            "groups": {"admin_chat": 70202},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["current_model_id"] = "model-1"
    rt["model_health_status"] = "down"
    rt["model_fallback_streak"] = 5
    rt["model_pause_active"] = True
    rt["model_probe_last_notify_at"] = 0

    sent = []
    sleep_calls = {"count": 0}

    class FakeModelManager:
        fallback_chain = ["1", "2"]
        models = [
            {"model_id": "model-1", "provider": "iflow", "enabled": True},
            {"model_id": "model-2", "provider": "iflow", "enabled": True},
        ]

        def get_model(self, key):
            mapping = {
                "1": self.models[0],
                "2": self.models[1],
                "model-1": self.models[0],
                "model-2": self.models[1],
            }
            return mapping.get(str(key))

        async def _call_iflow(self, model_cfg, messages, **kwargs):
            raise RuntimeError(f"{model_cfg['model_id']} timeout")

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", *args, **kwargs):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=70202, id=len(sent))

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            rt["model_pause_active"] = False
            rt["model_fallback_streak"] = 0
        return None

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())
    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm.asyncio, "sleep", fake_sleep)

    asyncio.run(zm._run_model_probe_loop(SimpleNamespace(), ctx, {}))

    assert any(msg_type == "model_failure" and "模型链仍不可用" in message for msg_type, message in sent)


def test_handle_goal_pause_after_settle_includes_account_and_gambling_funds(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "goal_pause_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "目标暂停用户"},
            "telegram": {"user_id": 70126},
            "groups": {"admin_chat": 70126},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["flag"] = True
    rt["period_profit"] = 1_200_000
    rt["profit"] = 1_000_000
    rt["profit_stop"] = 2
    rt["stop"] = 3
    rt["account_balance"] = 24_315_000
    rt["balance_status"] = "success"
    rt["gambling_fund"] = 21_654_000
    rt["current_round"] = 2
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260324_2_1", "amount": 20_000, "result": "赢", "profit": 19_800},
    ]

    sent = []

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, *args, **kwargs):
        sent.append((msg_type, message))
        return SimpleNamespace(chat_id=70126, id=len(sent))

    async def fake_refresh_pause_countdown_notice(client, user_ctx, global_cfg, remaining_rounds=None):
        return None

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm, "_refresh_pause_countdown_notice", fake_refresh_pause_countdown_notice)

    result = asyncio.run(zm._handle_goal_pause_after_settle(SimpleNamespace(), ctx, {}))

    assert result is True
    assert all(msg_type != "priority" for msg_type, _ in sent)
    goal_messages = [message for msg_type, message in sent if msg_type == "goal_pause"]
    assert goal_messages
    assert "账户资金：2431.50 万" in goal_messages[0]
    assert "菠菜资金：2165.40 万" in goal_messages[0]
    assert "本次暂停：2 局" in goal_messages[0]
    assert "系统已进入目标暂停" in goal_messages[0]


def test_build_fund_pause_message_uses_compact_alert_structure():
    message = zm._build_fund_pause_message(320000)

    assert "⛔ 资金不足，已暂停押注" in message
    assert "当前资金无法覆盖下一手下注" in message
    assert "当前剩余：32.00 万" in message
    assert "恢复方式：`gf [金额]`" in message


def test_res_bet_resets_current_chain_reconciliation(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "res_bet_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "重置押注用户"},
            "telegram": {"user_id": 70131},
            "groups": {"admin_chat": 70131},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_sequence_count"] = 4
    rt["lose_count"] = 4
    rt["bet_amount"] = 146_500
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260320_1_1", "amount": 10_000, "result": "输", "profit": -10_000},
        {"bet_id": "20260320_1_2", "amount": 28_500, "result": "输", "profit": -28_500},
        {"bet_id": "20260320_1_3", "amount": 66_000, "result": "输", "profit": -66_000},
        {"bet_id": "20260320_1_4", "amount": 146_500, "result": "输", "profit": -146_500},
    ]

    async def fake_send_to_admin(client, message, user_ctx, global_cfg):
        return SimpleNamespace(chat_id=70131, id=1)

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_to_admin", fake_send_to_admin)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="res bet", chat_id=70131, id=1)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    summary = zm.reconcile_bet_runtime_from_log(ctx)

    assert rt["bet_sequence_count"] == 0
    assert rt["lose_count"] == 0
    assert rt["bet_amount"] == rt["initial_amount"]
    assert rt["bet_reset_log_index"] == 4
    assert summary["continuous_count"] == 0
    assert summary["lose_count"] == 0


def test_res_bet_allows_new_chain_after_reset(tmp_path):
    user_dir = tmp_path / "users" / "res_bet_new_chain_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "重置后新链用户"},
            "telegram": {"user_id": 70132},
            "groups": {"admin_chat": 70132},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    rt = ctx.state.runtime
    rt["bet_reset_log_index"] = 2
    ctx.state.bet_sequence_log = [
        {"bet_id": "20260320_1_1", "amount": 10_000, "result": "输", "profit": -10_000},
        {"bet_id": "20260320_1_2", "amount": 28_500, "result": "输", "profit": -28_500},
        {"bet_id": "20260320_1_3", "amount": 10_000, "result": "输", "profit": -10_000},
    ]

    summary = zm.reconcile_bet_runtime_from_log(ctx)

    assert summary["continuous_count"] == 1
    assert summary["lose_count"] == 1
    assert rt["bet_sequence_count"] == 1
    assert rt["lose_count"] == 1


def test_extract_pattern_features_treats_4_streak_as_long_dragon():
    result = zm.extract_pattern_features([0, 1, 1, 1, 1])

    assert result["pattern_tag"] == "LONG_DRAGON"
    assert result["tail_streak_len"] == 4


def test_analyze_double_streak_followups_produces_directional_preference():
    history = []
    for _ in range(8):
        history.extend([0, 1, 1, 1, 0])
    history.extend([1, 1])

    result = zm.analyze_double_streak_followups(history)

    assert result["current_side"] == "big"
    assert result["current_side_total"] >= 8
    assert result["current_preference"] == "continue"
    assert result["current_continue_rate"] > result["current_reverse_rate"]


def test_analyze_rhythm_context_prefers_alternation_for_pure_single_jump():
    history = [0, 1] * 6

    result = zm.analyze_rhythm_context(history)

    assert result["rhythm_tag"] == "ALTERNATION_RHYTHM"
    assert result["alternation_score"] > result["pair_score"]
    assert result["alternation_next"] == 0


def test_detect_alternation_break_signal_uses_near_to_far_six_window():
    small_signal = zm._detect_alternation_break_signal([0, 1, 0, 1, 0, 1])
    assert small_signal["active"] is True
    assert small_signal["near_to_far_seq"] == "010101"
    assert small_signal["prediction"] == 0

    big_signal = zm._detect_alternation_break_signal([1, 0, 1, 0, 1, 0])
    assert big_signal["active"] is True
    assert big_signal["near_to_far_seq"] == "101010"
    assert big_signal["prediction"] == 1

    broken_signal = zm._detect_alternation_break_signal([0, 1, 1, 0, 1, 0])
    assert broken_signal["active"] is False


def test_build_predict_basis_text_uses_structured_multiline_copy():
    text = zm._build_predict_basis_text(
        history=[0, 1] * 20,
        prediction=0,
        source="model",
        pattern_tag="SINGLE_JUMP",
        rhythm_tag="ALTERNATION_RHYTHM",
        tail_streak_len=1,
        tail_streak_char=0,
    )

    assert text.startswith("🤖 决策依据\n")
    assert "├ 📊 100局：" in text
    assert "├ 🌊 40局：" in text
    assert "├ ⚡ 20局：" in text
    assert "├ 🧬 5局：" in text
    assert "├ 🤖 大模型：" in text
    assert "└ 🎯 押注结论： 本局坚决押【小】" in text


def test_build_predict_basis_text_hides_raw_parse_exception_for_fallback():
    text = zm._build_predict_basis_text(
        history=[0, 0, 1, 0, 0, 1, 0, 0],
        prediction=0,
        source="invalid_fallback",
        pattern_tag="INVALID_FALLBACK",
        rhythm_tag="CHAOS_NOISE",
        tail_streak_len=2,
        tail_streak_char=0,
    )

    assert "expecting value" not in text.lower()
    assert "解析兜底" not in text
    assert "├ 🤖 大模型： 模型这局没给稳定答案，先走系统兜底" in text
    assert "└ 🎯 押注结论： 本局坚决押【小】" in text


def test_build_ops_card_renders_multiline_block_without_duplicate_label():
    block = (
        "🤖 决策依据\n"
        "├ 📊 100局： 长期分布接近均衡\n"
        "├ 🌊 40局： 数量偏小（小比大多 4 次）\n"
        "├ ⚡ 20局： 短线来回切换，交替还在延续\n"
        "├ 🧬 5局： 5位单跳 [0 1 0 1 0]，还在来回跳\n"
        "├ 🤖 大模型： 交替结构更强，先顺着节奏看\n"
        "└ 🎯 押注结论： 本局坚决押【小】"
    )

    card = zm._build_ops_card(
        "示例标题",
        summary="示例摘要",
        fields=[
            ("⚡ 押注方向", "小"),
            ("", block),
        ],
    )

    assert "⚡ 押注方向：小" in card
    assert "🤖 决策依据" in card
    assert "：🤖 决策依据" not in card
    assert "└ 🎯 押注结论： 本局坚决押【小】" in card


def test_analyze_rhythm_context_prefers_pair_for_pair_formation_sequence():
    history = [1, 0, 1, 1, 0, 1, 1, 0, 1]

    result = zm.analyze_rhythm_context(history)

    assert result["rhythm_tag"] == "PAIR_FORMATION"
    assert result["pair_score"] > result["alternation_score"]
    assert result["pair_next"] == 1
    assert result["pair_would_form_double"] is True


def test_predict_next_bet_core_prompt_contains_rhythm_layer(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "rhythm_model_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "节奏提示词用户"},
            "telegram": {"user_id": 70141},
            "groups": {"admin_chat": 70141},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
            "ai": {
                "enabled": True,
                "api_keys": ["k1"],
                "models": {"1": {"model_id": "model-1", "enabled": True}},
                "fallback_chain": ["1"],
            },
        },
    )
    ctx = UserContext(str(user_dir))
    ctx.state.history = [1, 0, 1, 1, 0, 1, 1, 0, 1]
    ctx.state.runtime["current_model_id"] = "model-1"
    captured = {}

    class FakeModelManager:
        async def call_model(self, model_id, messages, **kwargs):
            captured["prompt"] = messages[1]["content"]
            return {
                "success": True,
                "error": "",
                "content": '{"prediction": 1, "confidence": 83, "reason": "pair rhythm"}',
                "model_id": "model-1",
                "requested_model_id": "model-1",
                "fallback_used": False,
            }

    monkeypatch.setattr(ctx, "get_model_manager", lambda: FakeModelManager())

    prediction = asyncio.run(zm.predict_next_bet_core(ctx, {}))

    assert prediction == 1
    assert "rhythm_tag" in captured["prompt"]
    assert "alternation_score" in captured["prompt"]
    assert "pair_score" in captured["prompt"]
    assert "pair_would_form_double" in captured["prompt"]
    assert "PAIR_FORMATION" in captured["prompt"]
    assert "near_term_40:" in captured["prompt"]


def test_format_dashboard_prioritizes_runtime_over_detail_sections(tmp_path):
    user_dir = tmp_path / "users" / "dashboard_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "仪表盘用户"},
            "telegram": {"user_id": 8808},
            "groups": {"admin_chat": 8808},
        },
    )

    ctx = UserContext(str(user_dir))
    ctx.state.history = [1, 0, 1, 1, 0, 0, 1, 1]
    ctx.state.runtime.update(
        {
            "bet_on": True,
            "current_preset_name": "yc05",
            "bet_mode": 2,
            "bet_amount": 2000,
            "account_balance": 3200000,
            "balance_status": "success",
            "gambling_fund": 2800000,
            "current_model_id": "model-1",
            "total": 18,
            "win_total": 10,
            "earnings": 360000,
            "period_profit": 120000,
        }
    )

    dashboard = zm.format_dashboard(ctx)

    assert dashboard.index("<b>【 状态监控 】</b>") < dashboard.index("<b>🎯 即时下注</b>")
    assert dashboard.index("<b>🎯 即时下注</b>") < dashboard.index("<b>💰 资产总览</b>")
    assert dashboard.index("<b>💰 资产总览</b>") < dashboard.index("<b>📊 近期 40 次结果（由近及远）</b>")
    assert dashboard.index("<b>📊 近期 40 次结果（由近及远）</b>") < dashboard.index("<b>⚙️ 策略参数</b>")
    assert "模式：追投" not in dashboard
    assert "├ 计划下注：" in dashboard
    assert "<pre>" not in dashboard
    assert "<blockquote>" not in dashboard


def test_status_command_sends_dashboard_with_html_parse_mode(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "status_cmd_user"
    _write_json(
        user_dir / "config.json",
        {
            "account": {"name": "状态命令用户"},
            "telegram": {"user_id": 6010},
            "groups": {"admin_chat": 6010},
            "notification": {"iyuu": {"enable": False}, "tg_bot": {"enable": False}},
        },
    )
    ctx = UserContext(str(user_dir))
    sent = {}

    async def fake_send_message_v2(client, msg_type, message, user_ctx, global_cfg, parse_mode="markdown", *args, **kwargs):
        sent["msg_type"] = msg_type
        sent["message"] = message
        sent["parse_mode"] = parse_mode
        return SimpleNamespace(chat_id=6010, id=1)

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(zm, "send_message_v2", fake_send_message_v2)
    monkeypatch.setattr(zm.asyncio, "create_task", fake_create_task)

    cmd_event = SimpleNamespace(raw_text="status", chat_id=6010, id=99)
    asyncio.run(zm.process_user_command(SimpleNamespace(), cmd_event, ctx, {}))

    assert sent["msg_type"] == "dashboard"
    assert sent["parse_mode"] == "html"
    assert "<b>【 状态监控 】</b>" in sent["message"]


def _dragon_extra_rt(**overrides):
    rt = {
        "edb": True,
        "win_count": 0,
        "lose_count": 0,
        "initial_amount": 5000,
        "bet_amount": 5000,
        "lose_stop": 13,
        "lose_once": 3.0,
        "lose_twice": 2.5,
        "lose_three": 2.2,
        "lose_four": 2.1,
        "dragon_extra_active": False,
    }
    rt.update(overrides)
    return rt


def test_dragon_extra_same_direction_triggers_on_five_streak():
    history = [0, 1, 1, 1, 1, 1]
    signal = zm._detect_dragon_extra_signal(history)
    assert signal["active"] is True
    assert signal["kind"] == "same"
    assert signal["direction"] == 1

    rt = _dragon_extra_rt()
    amount = zm._get_dragon_extra_bet_amount(rt, history)
    assert amount == 1_000_000
    assert rt["dragon_extra_active"] is True
    assert zm._apply_dragon_extra_direction(rt, history, 0) == 1
    assert zm.calculate_bet_amount(rt, history) == 1_005_000


def test_dragon_extra_alternation_triggers_on_five_streak():
    history = [1, 0, 1, 0, 1]
    signal = zm._detect_dragon_extra_signal(history)
    assert signal["active"] is True
    assert signal["kind"] == "alt"
    assert signal["direction"] == 0

    rt = _dragon_extra_rt()
    amount = zm._get_dragon_extra_bet_amount(rt, history)
    assert amount == 1_000_000
    assert zm._apply_dragon_extra_direction(rt, history, 1) == 0


def test_dragon_extra_keeps_going_after_win():
    history = [1, 1, 1, 1, 1]
    rt = _dragon_extra_rt()
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000

    history = history + [1]
    rt["win_count"] = 1
    rt["lose_count"] = 0
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    assert rt["dragon_extra_active"] is True
    assert zm.calculate_bet_amount(rt, history) == 1_005_000


def test_dragon_extra_stops_after_miss():
    history = [1, 1, 1, 1, 1]
    rt = _dragon_extra_rt()
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    zm._clear_dragon_extra_runtime(rt)
    broken = history + [0]
    assert zm._get_dragon_extra_bet_amount(rt) == 0
    assert zm._get_dragon_extra_bet_amount(rt, broken) == 0
    assert rt["dragon_extra_active"] is False
    assert zm.calculate_bet_amount(rt, broken) == 5_000


def test_dragon_extra_still_triggers_after_six_martingale_losses():
    history = [0, 1, 1, 1, 1, 1]
    rt = _dragon_extra_rt(lose_count=6, bet_amount=225_000)
    extra = zm._get_dragon_extra_bet_amount(rt, history)
    assert extra == 1_000_000
    assert rt["dragon_extra_active"] is True
    total = zm.calculate_bet_amount(rt, history)
    assert total > 1_000_000
    assert zm._apply_dragon_extra_direction(rt, history, 0) == 1
