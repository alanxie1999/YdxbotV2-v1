#!/usr/bin/env python3
"""完整下注逻辑模拟 - 包含长龙不中后重置逻辑"""

FIXED_PATTERNS = {
    "010101": {"follow": "reverse", "label": "交替循环反转"},
    "101010": {"follow": "reverse", "label": "交替循环反转"},
    "111111": {"follow": "1", "label": "大龙延续"},
    "000000": {"follow": "0", "label": "小龙延续"},
    "00101": {"follow": "reverse", "label": "00101反向下注"},
    "11010": {"follow": "reverse", "label": "11010反向下注"},
    "001010": {"follow": "same", "label": "001010同向下注"},
    "110101": {"follow": "same", "label": "110101同向下注"},
}


def _get_history_tail_streak(history):
    if not isinstance(history, list) or not history:
        return 0, -1
    tail_value = int(history[-1])
    streak = 1
    for idx in range(len(history) - 2, -1, -1):
        if int(history[idx]) != tail_value:
            break
        streak += 1
    return streak, tail_value


def _detect_fixed_pattern_signal(history):
    if not isinstance(history, list) or len(history) < 5:
        return {"active": False}
    history_str = "".join(str(x) for x in history)
    for pattern, config in FIXED_PATTERNS.items():
        pattern_len = len(pattern)
        if len(history) < pattern_len:
            continue
        recent_seq = history_str[-pattern_len:]
        if recent_seq == pattern:
            follow = config["follow"]
            latest = int(history[-1])
            if follow == "reverse":
                pred = 1 - latest
            elif follow == "same":
                pred = latest
            elif len(follow) == 1:
                pred = int(follow)
            else:
                pred = latest
            return {"active": True, "detected_seq": recent_seq,
                    "follow_pattern": follow, "label": config["label"],
                    "prediction": pred}
    return {"active": False}


def _get_dragon_extra(rt, history):
    if rt.get("lose_count", 0) > 0:
        rt["dragon_extra_active"] = False
        return 0
    if not isinstance(history, list) or len(history) < 6:
        rt["dragon_extra_active"] = False
        return 0
    streak, _ = _get_history_tail_streak(history)
    if streak >= 6:
        rt["dragon_extra_active"] = True
        return 250000
    if rt.get("dragon_extra_active", False):
        return 250000
    return 0


def get_prediction(history, rt):
    # 优先级 1: 固定规律
    fixed = _detect_fixed_pattern_signal(history)
    if fixed.get("active"):
        return fixed["prediction"], fixed["label"], fixed["follow_pattern"], fixed["detected_seq"]
    
    # 优先级 2: 5 位交替打破
    if len(history) >= 5:
        last_5 = "".join(str(x) for x in history[-5:])
        if last_5 in ("10101", "01010"):
            pred = 1 - history[-1]
            return pred, f"5 位交替{last_5}反向", "reverse", last_5
    
    # 优先级 3: 跟随上一手
    if history:
        return history[-1], "跟随上一手", "follow", ""
    
    return 1, "无历史默认大", "default", ""


def simulate(history_sequence, description="", initial_amount=500):
    print(f"\n{'='*70}")
    print(f"测试: {description}")
    print(f"序列: {' '.join(str(x) for x in history_sequence)}\n")
    
    rt = {
        "lose_count": 0, "win_count": 0, "bet_amount": initial_amount,
        "initial_amount": initial_amount, "dragon_extra_active": False,
        "total_bet": 0, "total_win": 0, "total_extra": 0, "dragon_bet_count": 0
    }
    
    for i in range(len(history_sequence)):
        hist = history_sequence[:i]
        actual = history_sequence[i]
        
        pred, label, follow, seq = get_prediction(hist, rt)
        extra = _get_dragon_extra(rt, hist)
        streak, side = _get_history_tail_streak(hist)
        
        current_bet = rt["bet_amount"] + extra
        match = pred == actual
        m = "✓" if match else "✗"
        pt = "大" if pred == 1 else "小"
        at = "大" if actual == 1 else "小"
        ex = f" +25万(龙尾{streak}连)" if extra > 0 else ""
        
        print(f"  第{i+1:2d}手: {pt} -> {at} {m} [{label}]{ex} (下注{current_bet})")
        
        if match:
            rt["win_count"] = rt.get("win_count", 0) + 1
            rt["lose_count"] = 0
            win_amount = rt["bet_amount"] * 0.99
            rt["total_win"] += win_amount + extra
            rt["bet_amount"] = initial_amount
            if extra > 0:
                rt["dragon_bet_count"] += 1
        else:
            rt["lose_count"] += 1
            rt["win_count"] = 0
            # 长龙额外加注不中后，按默认金额下注
            if rt.get("dragon_extra_active", False):
                rt["bet_amount"] = initial_amount
                rt["dragon_extra_active"] = False
                print(f"         -> 龙尾不中，重置为默认金额{initial_amount}")
            else:
                rt["bet_amount"] = rt["bet_amount"] * 2.1
        
        rt["total_bet"] += current_bet
        rt["total_extra"] += extra
    
    net = rt["total_win"] - rt["total_bet"]
    print(f"\n  统计: 总下注={rt['total_bet']:.0f}, 总赢={rt['total_win']:.0f}, 净盈亏={net:.0f}")
    print(f"  长龙加注: 总额={rt['total_extra']}, 成功次数={rt.get('dragon_bet_count', 0)}")


print("完整下注逻辑模拟")
print("="*70)

# 测试 1: 6 连大后额外加注，不中后重置
simulate([1, 1, 1, 1, 1, 1, 1, 0, 1], "6 连大后额外加注，第 8 手不中重置")

# 测试 2: 长龙连续中后不中
simulate([1, 1, 1, 1, 1, 1, 1, 1, 1, 0], "9 连大后不中")

# 测试 3: 001010 同向
simulate([0, 0, 1, 0, 1, 0, 0], "001010 同向下注")

# 测试 4: 00101 反向
simulate([0, 0, 1, 0, 1, 0], "00101 反向下注")
