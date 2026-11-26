"""Tiny mean-reversion bot for make.fun's "Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana".

Strategy: hourly mean reversion, long only. For each of the seven assets, buy
an oversold dip (RSI(14) below 35 while the price sits under its 24-hour EMA)
at 12% of equity, and sell on an overbought rip (RSI above 65), a 6%
take-profit, or a 5% stop-loss from the fill price. The bot holds at most one
position per asset, never shorts, never uses leverage, and sizes every order
under the 25% weight cap. Fully deterministic: every decision is a pure
function of the bars received; no clock, no network, no randomness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SLOW_LEN = 24
RSI_LEN = 14
RSI_BUY_MAX = 35.0
RSI_SELL_MIN = 65.0
TAKE_PROFIT_PCT = 0.06
STOP_LOSS_PCT = 0.05
TARGET_WEIGHT_BPS = 1200
BUY_SLIPPAGE_BPS = 1000
SELL_SLIPPAGE_BPS = 2000
MIN_HISTORY = SLOW_LEN + 1


class DipBot:
    """Keeps per-symbol price history, EMA(24) and RSI(14) state."""

    def __init__(self):
        self.closes = {}
        self.slow = {}
        self.avg_gain = {}
        self.avg_loss = {}
        self.prev_close = {}
        self.entry_price = {}

    def _alpha(self, length):
        return 2.0 / (length + 1.0)

    def note_bar(self, symbol, price):
        hist = self.closes.setdefault(symbol, [])
        hist.append(price)
        if symbol not in self.slow:
            self.slow[symbol] = price
            self.avg_gain[symbol] = 0.0
            self.avg_loss[symbol] = 0.0
        else:
            prev = self.prev_close[symbol]
            self.slow[symbol] += self._alpha(SLOW_LEN) * (price - self.slow[symbol])
            change = price - prev
            gain = change if change > 0 else 0.0
            loss = -change if change < 0 else 0.0
            if len(hist) <= RSI_LEN + 1:
                self.avg_gain[symbol] += gain / RSI_LEN
                self.avg_loss[symbol] += loss / RSI_LEN
            else:
                k = 1.0 / RSI_LEN
                self.avg_gain[symbol] = (1.0 - k) * self.avg_gain[symbol] + k * gain
                self.avg_loss[symbol] = (1.0 - k) * self.avg_loss[symbol] + k * loss
        self.prev_close[symbol] = price

    def rsi(self, symbol):
        if len(self.closes.get(symbol, [])) <= RSI_LEN:
            return 50.0
        loss = self.avg_loss[symbol]
        if loss == 0.0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + self.avg_gain[symbol] / loss)

    def ready(self, symbol):
        return len(self.closes.get(symbol, [])) >= MIN_HISTORY

    def buy_signal(self, symbol, price):
        if not self.ready(symbol):
            return False
        if price >= self.slow[symbol]:
            return False
        return self.rsi(symbol) < RSI_BUY_MAX

    def sell_signal(self, symbol, price):
        entry = self.entry_price.get(symbol)
        if entry is not None and price >= entry * (1.0 + TAKE_PROFIT_PCT):
            return True
        if entry is not None and price < entry * (1.0 - STOP_LOSS_PCT):
            return True
        return self.rsi(symbol) > RSI_SELL_MIN


BOT = DipBot()


def price_of(state, symbol):
    close = state.close(symbol)
    if close is None:
        return None
    return close / 10 ** state.symbols[symbol]["price_scale"]


def on_hello(state):
    BOT.__init__()


def on_batch(state, batch):
    for bar in batch["bars"]:
        price = price_of(state, bar["symbol"])
        if price is not None:
            BOT.note_bar(bar["symbol"], price)
    for fill in batch["fills"]:
        if fill["status"] != "filled":
            continue
        symbol = fill["symbol"]
        if fill["side"] == "buy":
            BOT.entry_price[symbol] = int(fill["price"]) / 10 ** state.symbols[symbol]["price_scale"]
        else:
            BOT.entry_price.pop(symbol, None)
    if state.warming_up:
        return []
    orders = []
    sold = set()
    for symbol, holding in sorted(state.positions.items()):
        price = price_of(state, symbol)
        if price is None:
            continue
        if BOT.sell_signal(symbol, price):
            orders.append({"side": "sell", "symbol": symbol, "qty": holding.qty,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            sold.add(symbol)
    planned_cash = state.cash
    for symbol in sorted(state.symbols):
        if symbol in state.positions or symbol in sold:
            continue
        price = price_of(state, symbol)
        if price is None:
            continue
        if not BOT.buy_signal(symbol, price):
            continue
        target = TARGET_WEIGHT_BPS * state.equity // 10000
        room = state.weight_room(symbol)
        notional = target if target < room else room
        if notional > planned_cash:
            notional = planned_cash
        if notional < int(state.hello["fees"]["min_notional_quote_units"]):
            continue
        orders.append({"side": "buy", "symbol": symbol,
                       "notional_quote_units": notional,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        planned_cash -= notional
    return orders


def check_replay_consistent():
    """Self-check: replay a fixed synthetic price path twice through on_batch,
    simulating fills locally so buys and sells both execute, and confirm the
    two reply sequences are identical and every order is well-formed (buys at
    or above the minimum notional, sells within the held quantity). Runs after
    the real replay, prints to stderr only so the reply stream on stdout
    stays clean."""
    import sys as _sys

    closes = list(range(100, 75, -1)) + list(range(77, 92))
    hello = {
        "type": "hello",
        "protocol": makefun_bars.PROTOCOL,
        "seed": "7",
        "symbols": [{"symbol": "SOL", "mint": "So11111111111111111111111111111111111111112",
                     "price_scale": 2, "qty_scale": 6}],
        "capital": {"quote": "USDC", "quote_scale": 6, "quote_units": "10000000000"},
        "bar_interval": "1h",
        "first_close_ms": 1793235600000,
        "caps": {"max_open_positions": 10, "max_orders_per_bar": 16,
                 "max_weight_bps": 2500, "shorts": False, "leverage": False},
        "fees": {"fee_bps": 10, "slippage_bps": 5,
                 "min_notional_quote_units": "1000000"},
        "latency_bars": 1,
    }

    def one_pass():
        BOT.__init__()
        state = makefun_bars.BarsState(hello)
        replies = []
        pending_fills = []
        for seq, close in enumerate(closes):
            raw = close * 100
            batch = {
                "type": "batch", "seq": seq, "t": 1793235600000 + seq * 3600000,
                "bars": [{"symbol": "SOL", "t": 1793235600000 + seq * 3600000,
                          "o": str(raw), "h": str(raw), "l": str(raw), "c": str(raw),
                          "v": None, "o_depth": ["9000000000000"],
                          "c_depth": ["9000000000000"]}],
                "fills": pending_fills,
                "account": {"cash_quote_units": str(state.cash),
                            "positions": [{"symbol": s, "qty": str(h.qty),
                                         "cost_quote_units": str(h.cost)}
                                        for s, h in sorted(state.positions.items())],
                            "equity_quote_units": str(state.equity)},
            }
            state.apply_batch(batch)
            orders = state.to_orders(on_batch(state, batch))
            pending_fills = []
            for order in orders:
                if order["side"] == "buy":
                    notional = int(order["notional_quote_units"])
                    assert notional >= 1000000, "buy below minimum notional"
                    spend = notional * 9990 // 10000
                    qty = spend * 10 ** (6 + 2) // (raw * 10 ** 6)
                    assert qty > 0, "buy quantity truncated to zero"
                    state.cash -= notional
                    old = state.positions.get("SOL")
                    if old is not None:
                        state.positions["SOL"] = makefun_bars.Holding(old.qty + qty, old.cost + notional)
                    else:
                        state.positions["SOL"] = makefun_bars.Holding(qty, notional)
                    pending_fills.append({"client_id": order["client_id"], "symbol": "SOL",
                                          "side": "buy", "status": "filled", "reason": None,
                                          "decided_bar": batch["t"], "fill_bar": batch["t"],
                                          "price": str(raw), "qty": str(qty),
                                          "notional_quote_units": str(notional),
                                          "fee_quote_units": str(notional - spend)})
                else:
                    held = state.positions.get(order["symbol"])
                    assert held is not None and int(order["qty"]) <= held.qty, "sell exceeds holding"
                    qty = int(order["qty"])
                    gross = qty * raw * 10 ** 6 // 10 ** (6 + 2)
                    proceeds = gross * 9990 // 10000
                    state.cash += proceeds
                    if qty == held.qty:
                        del state.positions[order["symbol"]]
                    else:
                        state.positions[order["symbol"]] = makefun_bars.Holding(
                            held.qty - qty, held.cost * (held.qty - qty) // held.qty)
                    pending_fills.append({"client_id": order["client_id"], "symbol": "SOL",
                                          "side": "sell", "status": "filled", "reason": None,
                                          "decided_bar": batch["t"], "fill_bar": batch["t"],
                                          "price": str(raw), "qty": str(qty),
                                          "notional_quote_units": str(proceeds),
                                          "fee_quote_units": str(gross - proceeds)})
            replies.append(makefun_bars.encode_reply(seq, orders))
        return replies

    first = one_pass()
    second = one_pass()
    assert first == second, "replay is not deterministic"
    parsed = [__import__("json").loads(line)["orders"] for line in first]
    buys = sum(1 for orders in parsed for o in orders if o["side"] == "buy")
    sells = sum(1 for orders in parsed for o in orders if o["side"] == "sell")
    assert buys >= 1 and sells >= 1, "synthetic path exercised no round trip"
    print(f"self-check ok: {len(first)} batches, {buys} buys, {sells} sells, "
          "identical across two passes", file=_sys.stderr)


def main():
    makefun_bars.run(on_batch, on_hello=on_hello)
    check_replay_consistent()


if __name__ == "__main__":
    main()
