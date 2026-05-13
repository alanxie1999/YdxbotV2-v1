#!/usr/bin/env python3
"""下注逻辑完整模拟测试 - 集成固定规律后"""

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
    
    rt = {"lose_count": 0, "bet_amount": initial_amount,
          "dragon_extra_active": False, "total_bet": 0,
          "total_win": 0, "total_extra": 0}
    
    for i in range(len(history_sequence)):
        hist = history_sequence[:i]
        actual = history_sequence[i]
        
        pred, label, follow, seq = get_prediction(hist, rt)
        extra = _get_dragon_extra(rt, hist)
        streak, _ = _get_history_tail_streak(hist)
        
        match = pred == actual
        m = "✓" if match else "✗"
        pt = "大" if pred == 1 else "小"
        at = "大" if actual == 1 else "小"
        ex = f" +25万(龙尾{streak}连)" if extra > 0 else ""
        
        print(f"  第{i+1:2d}手: {pt} -> {at} {m} [{label}]{ex}")
        
        if match:
            rt["win_count"] = rt.get("win_count", 0) + 1
            rt["lose_count"] = 0
            rt["total_win"] += rt["bet_amount"] * 0.99
            rt["bet_amount"] = initial_amount
        else:
            rt["lose_count"] += 1
            rt["win_count"] = 0
            rt["bet_amount"] = rt["bet_amount"] * 2.1
        
        rt["total_bet"] += rt["bet_amount"] + extra
        rt["total_extra"] += extra
    
    net = rt["total_win"] - rt["total_bet"]
    print(f"\n  总下注={rt['total_bet']:.0f}, 总赢={rt['total_win']:.0f}, 净盈亏={net:.0f}, 额外加注={rt['total_extra']}")


print("下注逻辑集成测试 (固定规律 + 交替打破 + 长龙加注)")
print("="*70)

simulate([0, 0, 1, 0, 1, 0], "00101 反向下注")
simulate([0, 0, 1, 0, 1, 0, 0], "001010 同向下注")
simulate([1, 1, 0, 1, 0, 1], "11010 反向下注")
simulate([1, 1, 0, 1, 0, 1, 1], "110101 同向下注")
simulate([0, 1, 0, 1, 0, 1, 1], "010101 交替循环反转")
simulate([1, 1, 1, 1, 1, 1, 1, 0], "6 连大后额外加注")
simulate([1, 1, 1, 1, 1, 1, 0], "6 连大后中断")
