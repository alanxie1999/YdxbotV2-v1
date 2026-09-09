#!/usr/bin/env python3
import zq_multiuser as zm


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


def test_same_direction():
    history = [0, 1, 1, 1, 1, 1]
    signal = zm._detect_dragon_extra_signal(history)
    assert signal["active"] is True
    assert signal["kind"] == "same"
    assert signal["direction"] == 1
    rt = _dragon_extra_rt()
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    assert rt["dragon_extra_active"] is True
    assert zm._apply_dragon_extra_direction(rt, history, 0) == 1
    assert zm.calculate_bet_amount(rt, history) == 1_005_000
    print("ok same")


def test_alternation():
    history = [1, 0, 1, 0, 1]
    signal = zm._detect_dragon_extra_signal(history)
    assert signal["active"] is True
    assert signal["kind"] == "alt"
    assert signal["direction"] == 0
    rt = _dragon_extra_rt()
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    assert zm._apply_dragon_extra_direction(rt, history, 1) == 0
    print("ok alt")


def test_keep_after_win():
    history = [1, 1, 1, 1, 1]
    rt = _dragon_extra_rt()
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    history = history + [1]
    rt["win_count"] = 1
    rt["lose_count"] = 0
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    assert rt["dragon_extra_active"] is True
    assert zm.calculate_bet_amount(rt, history) == 1_005_000
    print("ok win continue")


def test_stop_after_miss():
    history = [1, 1, 1, 1, 1]
    rt = _dragon_extra_rt()
    assert zm._get_dragon_extra_bet_amount(rt, history) == 1_000_000
    zm._clear_dragon_extra_runtime(rt)
    broken = history + [0]
    assert zm._get_dragon_extra_bet_amount(rt) == 0
    assert zm._get_dragon_extra_bet_amount(rt, broken) == 0
    assert rt["dragon_extra_active"] is False
    assert zm.calculate_bet_amount(rt, broken) == 5_000
    print("ok miss stop")


def test_after_six_losses():
    history = [0, 1, 1, 1, 1, 1]
    rt = _dragon_extra_rt(lose_count=6, bet_amount=225_000)
    extra = zm._get_dragon_extra_bet_amount(rt, history)
    assert extra == 1_000_000
    assert rt["dragon_extra_active"] is True
    total = zm.calculate_bet_amount(rt, history)
    assert total > 1_000_000
    assert zm._apply_dragon_extra_direction(rt, history, 0) == 1
    print("ok after 6 losses")


if __name__ == "__main__":
    test_same_direction()
    test_alternation()
    test_keep_after_win()
    test_stop_after_miss()
    test_after_six_losses()
    print("all passed")
