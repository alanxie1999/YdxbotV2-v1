"""
模拟交替下注策略测试
策略：
- 跟随上一手：历史最后一手是 1 押大，是 0 押小
- 5 位交替检测：最近 5 手是 10101 或 01010 时反向打破
"""

def simulate_bet(history):
    """根据历史模拟下注决策"""
    if len(history) == 0:
        return 1, "无历史数据，默认下大"
    
    if len(history) >= 5:
        last_5 = "".join(str(x) for x in history[-5:])
        if last_5 in ("10101", "01010"):
            prediction = 1 - history[-1]
            return prediction, f"5 位纯交替{last_5}，第 5 注后反向下注{'大' if prediction == 1 else '小'}"
    
    # 跟随上一手
    prediction = history[-1]
    return prediction, f"跟随上一手{history[-1]}，下{'大' if prediction == 1 else '小'}"


def run_simulation(test_cases):
    """运行模拟测试"""
    print("=" * 80)
    print("交替下注策略模拟测试")
    print("=" * 80)
    
    for i, (name, history_seq) in enumerate(test_cases, 1):
        print(f"\n【测试{i}: {name}】")
        print(f"初始历史：{history_seq}")
        print(f"{'-' * 80}")
        print(f"{'局数':<6} {'开奖':<6} {'历史 (最近 5 手)':<20} {'预测':<6} {'下注':<6} {'结果':<6} {'说明'}")
        print(f"{'-' * 80}")
        
        history = list(history_seq)
        balance = 0
        bet_amount = 100
        win_count = 0
        lose_count = 0
        
        # 模拟 15 局
        for round_num in range(15):
            prediction, reason = simulate_bet(history)
            bet_side = "大" if prediction == 1 else "小"
            
            # 模拟开奖（使用预设的交替模式）
            if "交替" in name or "单跳" in name:
                result = (round_num + len(history_seq)) % 2  # 交替开奖
            elif "龙" in name:
                result = 1 if round_num % 3 == 0 else 1  # 长龙
            else:
                result = round_num % 2  # 默认交替
            
            result_side = "大" if result == 1 else "小"
            
            # 判断输赢
            win = (prediction == result)
            if win:
                balance += bet_amount * 0.98
                win_count += 1
                result_text = f"✅ 赢 +{bet_amount * 0.98:.0f}"
            else:
                balance -= bet_amount
                lose_count += 1
                result_text = f"❌ 输 -{bet_amount}"
            
            # 显示历史（最近 5 手）
            hist_display = "".join(str(x) for x in history[-5:]) if len(history) >= 5 else "".join(str(x) for x in history)
            
            print(f"{round_num + 1:<6} {result_side:<6} {hist_display:<20} {bet_side:<6} {'大' if prediction == 1 else '小':<6} {result_text:<12} {reason[:20]}")
            
            # 添加结果到历史
            history.append(result)
        
        print(f"{'-' * 80}")
        print(f"最终统计：赢{win_count}局 | 输{lose_count}局 | 净盈利：{balance:.0f}")
        print()


# 测试场景
test_cases = [
    ("完美交替 - 从空历史开始", []),
    ("完美交替 - 从 1010 开始", [1, 0, 1, 0]),
    ("完美交替 - 从 0101 开始", [0, 1, 0, 1]),
    ("长龙 - 连续大", [1, 1, 1]),
    ("长龙 - 连续小", [0, 0, 0]),
    ("随机混合", [1, 1, 0, 1, 0, 0, 1]),
]

run_simulation(test_cases)

print("=" * 80)
print("模拟完成")
print("=" * 80)
