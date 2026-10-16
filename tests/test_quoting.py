from kbtc.pricing import digital_up, sigma_per_sqrt_second
from kbtc.quoting import near_strike_endgame, quote

SIG = sigma_per_sqrt_second(0.50)


def test_flat_inventory_quotes_straddle_fair():
    v = digital_up(60_000, 60_000, 900, SIG)
    q = quote(v.prob_up, v.delta_x, SIG, inventory=0)
    assert q.bid_cents < 50 < q.ask_cents


def test_long_inventory_skews_quotes_down():
    v = digital_up(60_000, 60_000, 900, SIG)
    flat = quote(v.prob_up, v.delta_x, SIG, inventory=0)
    long = quote(v.prob_up, v.delta_x, SIG, inventory=20)
    assert long.reservation < flat.reservation
    assert long.ask_cents <= flat.ask_cents and long.bid_cents <= flat.bid_cents


def test_spread_wider_at_the_money_than_in_the_tail():
    atm = digital_up(60_000, 60_000, 300, SIG)
    tail = digital_up(60_000, 60_600, 300, SIG)
    assert quote(atm.prob_up, atm.delta_x, SIG, 0).half_spread > quote(tail.prob_up, tail.delta_x, SIG, 0).half_spread


def test_quotes_outside_valid_range_are_dropped():
    deep = digital_up(60_000, 61_500, 60, SIG)  # ~0
    q = quote(deep.prob_up, deep.delta_x, SIG, 0)
    assert q.bid_cents is None and q.ask_cents is not None


def test_endgame_rule():
    assert near_strike_endgame(60, 0.3)
    assert not near_strike_endgame(60, 2.5)
    assert not near_strike_endgame(600, 0.3)
