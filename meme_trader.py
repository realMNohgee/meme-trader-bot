# Meme Coin Day Trader Bot by Grok - Version 3.7.1
# Uses KuCoin exchange for price data and simulation (fast, no location restrictions).
# Selects top 5 meme coins by 24h volume on KuCoin via /api/v1/market/allTickers, filtered from known memes (no fallback).
# Allocates $1 per trade, no cap on total investment per coin to allow thousands of trades/hour.
# Sells for small profits initially (0.001% above EMA); if profit >50% of invested, increases sell threshold to 0.025%.
# Title screen with ASCII art flair (inspired by retro terminal games like Asteroids/Pac-Man borders).
# Main dashboard with borders, colors, and simple meme icons; shows live deviation and total value.
# Fully automated trading (only q to quit or r to reset); INITIAL_BALANCE=50.0; sim fees (0.1% per trade).
# KuCoin WebSocket: Fetches public token, connects to dynamic endpoint, subscribes to /market/ticker:SYMBOL-USDT individually with delays, with CCXT fallback.
# Changes from v3.7.0: Individual ticker subscriptions with delays, removed MAX_ALLOC_PER_COIN, added CCXT price fallback, added reconnect logging.
import requests
import websocket
import json
import threading
import time
import curses
import ccxt
import logging

# Section: Logging Setup
logging.basicConfig(filename='bot_logs.txt', level=logging.DEBUG,
                    format='%(asctime)s - %(message)s')

# Section: Configuration
SIMULATION_MODE = True
INITIAL_BALANCE = 50.0
INITIAL_BUY_USD = 1.0
TRADE_AMOUNT_USD = 1.0
THRESHOLD_PCT_BUY = 0.001  # Buy if below EMA by 0.001%
THRESHOLD_PCT_SELL_BASE = 0.001  # Sell if above EMA by 0.001%
THRESHOLD_PCT_SELL_HIGH = 0.025
PROFIT_HIGH_MARK = 0.5
EMA_PERIOD = 30
NUM_COINS_TO_TRACK = 5
FEE_PCT = 0.001  # 0.1% fee per trade
PADDING = 2
MAX_INITIAL_RETRIES = 20
WEBSOCKET_RECONNECT_DELAY = 5
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2
COOLDOWN_SEC = 5
THRESHOLD_TOLERANCE = 0.0001
TOKEN_RETRY_ATTEMPTS = 3

# Section: Global Data
prices = {}
changes = {}
price_histories = {}
emas = {}
last_trade_times = {}
balances = {'USDT': INITIAL_BALANCE}
invested = {}
coin_profits = {}
trade_logs = []
total_profit = 0.0
total_trades = 0
debug_logs = []
deviations = {}
ws = None

# Section: Exchange Setup
exchange = ccxt.kucoin()

# Section: Coin Selection
MEME_COINS = {'DOGE', 'SHIB', 'PEPE', 'BONK', 'FLOKI', 'WIF', 'BRETT', 'MOG', 'CRO', 'GME', 'TRUMP', 'BOME', 'DEGEN', 'MEW', 'SLERF', 'MYRO', 'MAGA', 'TURBO', 'MOTHER', 'KITTY'}
def get_top_volume_meme_coins():
    url = "https://api.kucoin.com/api/v1/market/allTickers"
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            if 'data' in data and 'ticker' in data['data']:
                break
            else:
                raise Exception("Invalid response format")
        except Exception as e:
            print(f"Error fetching KuCoin allTickers (attempt {attempt + 1}): {e}")
            logging.error(f"KuCoin allTickers fetch failed (attempt {attempt + 1}): {e}")
            if attempt < API_RETRY_ATTEMPTS - 1:
                time.sleep(API_RETRY_DELAY)
            else:
                trade_logs.append("KuCoin allTickers fetch failed after retries - no coins selected.")
                logging.error("KuCoin allTickers fetch failed after retries.")
                return []
    candidates = []
    for item in data['data']['ticker']:
        symbol_pair = item['symbol']
        if symbol_pair.endswith('-USDT'):
            symbol = symbol_pair.replace('-USDT', '')
            if symbol in MEME_COINS:
                candidates.append({
                    'name': symbol,
                    'symbol': symbol,
                    'id': symbol.lower(),
                    'kucoin_symbol': symbol_pair,
                    '24h_change': float(item.get('changeRate', 0)) * 100,
                    'volValue': float(item.get('volValue', 0))
                })
                logging.debug(f"Volume candidate: {symbol} (volume: {item['volValue']}, change: {item['changeRate']})")
            else:
                logging.debug(f"Skipped {symbol} - not a meme coin")
    candidates.sort(key=lambda x: x['volValue'], reverse=True)
    selected = candidates[:NUM_COINS_TO_TRACK]
    if selected:
        log = f"Selected top {len(selected)} meme coins by volume on KuCoin: {', '.join(c['symbol'] for c in selected)} 📈🚀"
        trade_logs.append(log)
        logging.info(log)
    else:
        log = "No meme coins with volume available on KuCoin - no trades will occur. Check bot_logs.txt for details."
        trade_logs.append(log)
        logging.warning(log)
    return selected
coins = get_top_volume_meme_coins()

# Section: Data Initialization
for coin in coins:
    symbol = coin['symbol']
    prices[symbol] = 0.0
    changes[symbol] = 0.0
    price_histories[symbol] = []
    emas[symbol] = 0.0
    last_trade_times[symbol] = 0
    balances[symbol] = 0.0
    invested[symbol] = 0.0
    coin_profits[symbol] = 0.0
    deviations[symbol] = 0.0

# Section: KuCoin WebSocket Setup
def get_kucoin_token():
    url = "https://api.kucoin.com/api/v1/bullet-public"
    for attempt in range(TOKEN_RETRY_ATTEMPTS):
        try:
            response = requests.post(url)
            response.raise_for_status()
            data = response.json()
            if 'data' in data and 'token' in data['data'] and 'instanceServers' in data['data']:
                token = data['data']['token']
                endpoint = data['data']['instanceServers'][0]['endpoint']
                ping_interval = 15  # KuCoin recommends 18s, use 15s for safety
                logging.info(f"Successfully fetched KuCoin token: {token[:10]}..., endpoint: {endpoint}, ping_interval: {ping_interval}s")
                return token, endpoint, ping_interval
            else:
                raise Exception("Invalid response format")
        except Exception as e:
            print(f"Error fetching KuCoin token (attempt {attempt + 1}): {e}")
            logging.error(f"KuCoin token fetch failed (attempt {attempt + 1}): {e}")
            if attempt < TOKEN_RETRY_ATTEMPTS - 1:
                time.sleep(API_RETRY_DELAY)
            else:
                raise Exception("Failed to fetch KuCoin public token after retries")
    return None, None, None

# Section: Fallback Price Fetch
def fetch_fallback_prices():
    while True:
        for coin in coins:
            symbol = coin['symbol']
            symbol_pair = coin['kucoin_symbol']
            if prices.get(symbol, 0.0) == 0.0:
                try:
                    ticker = exchange.fetch_ticker(symbol_pair)
                    price = float(ticker['last'])
                    prices[symbol] = price
                    logging.debug(f"Fetched fallback price for {symbol}: ${price:.10f}")
                except Exception as e:
                    logging.error(f"Failed to fetch fallback price for {symbol_pair}: {e}")
        time.sleep(60)  # Fetch every minute

fallback_thread = threading.Thread(target=fetch_fallback_prices)
fallback_thread.daemon = True
fallback_thread.start()

# Section: WebSocket Handling
def on_message(ws, message):
    logging.debug(f"Raw WebSocket message: {message}")
    data = json.loads(message)
    if 'type' in data and data['type'] == 'pong':
        logging.debug("Received WebSocket pong")
    if 'topic' in data and 'data' in data and data['topic'].startswith('/market/ticker:'):
        payload = data['data']
        symbol_pair = data['topic'].split(':')[1]
        symbol = symbol_pair.replace('-USDT', '')
        price = float(payload['price'])
        change = 0.0
        if symbol in prices and prices[symbol] > 0:
            change = (price - prices[symbol]) / prices[symbol] * 100
        prices[symbol] = price
        changes[symbol] = change
        if symbol in price_histories:
            price_histories[symbol].append(price)
            if len(price_histories[symbol]) > EMA_PERIOD:
                price_histories[symbol].pop(0)
            emas[symbol] = sum(price_histories[symbol]) / len(price_histories[symbol]) if price_histories[symbol] else price
            deviations[symbol] = (price - emas[symbol]) / emas[symbol] * 100 if emas[symbol] > 0 else 0.0
            logging.debug(f"{symbol} price: ${price:.10f}, EMA: ${emas[symbol]:.10f}, deviation: {deviations[symbol]:.2f}%")
            logging.debug(f"Balance {symbol}: {balances[symbol]:.4f}, USDT: {balances['USDT']:.2f}")

def on_error(ws, error):
    print(f"WebSocket error: {error}")
    logging.error(f"WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed")
    logging.info(f"WebSocket closed (code: {close_status_code}, msg: {close_msg}) - reconnecting...")
    time.sleep(WEBSOCKET_RECONNECT_DELAY)
    run_ws()

def on_open(ws):
    log = "WebSocket opened - subscribing to tickers..."
    debug_logs.append(log)
    logging.info(log)
    for coin in coins:
        subs = {
            "type": "subscribe",
            "topic": f"/market/ticker:{coin['kucoin_symbol']}"
        }
        ws.send(json.dumps(subs))
        logging.debug(f"Subscribed to {coin['kucoin_symbol']}")
        time.sleep(1)  # Delay to avoid KuCoin limits

# Section: WebSocket Runner
def run_ws():
    global ws
    token, endpoint, ping_interval = get_kucoin_token()
    ws_url = f"{endpoint}?token={token}"
    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    while True:
        ws.run_forever(ping_interval=ping_interval)
        logging.info("WebSocket closed - reconnecting...")
        time.sleep(WEBSOCKET_RECONNECT_DELAY)

thread = threading.Thread(target=run_ws)
thread.daemon = True
thread.start()
time.sleep(10)

# Section: Trade Execution
def execute_trade(symbol, side, amount_usd):
    global total_profit, total_trades
    price = prices.get(symbol, 0.0)
    if price == 0.0:
        log = f"Cannot {side} {symbol} - price not loaded yet!"
        debug_logs.append(log)
        logging.warning(log)
        return
    quantity = amount_usd / price
    fee = amount_usd * FEE_PCT
    if side == 'buy':
        if balances['USDT'] < amount_usd + fee:
            log = f"Cannot buy more {symbol} - insufficient USDT!"
            debug_logs.append(log)
            logging.warning(log)
            return
        balances['USDT'] -= (amount_usd + fee)
        balances[symbol] += quantity
        invested[symbol] += amount_usd
        log = f"Bought {quantity:.4f} {symbol} for ${amount_usd:.2f} (fee ${fee:.4f})! HODL! 💪"
        debug_logs.append(log)
        logging.info(log)
    elif side == 'sell':
        if balances[symbol] < quantity:
            log = f"Not enough {symbol} to sell!"
            debug_logs.append(log)
            logging.warning(log)
            return
        balances['USDT'] += (amount_usd - fee)
        balances[symbol] -= quantity
        profit = amount_usd - (quantity * (emas[symbol] or price)) - fee
        coin_profits[symbol] += profit
        total_profit += profit
        log = f"Sold {quantity:.4f} {symbol} for ${amount_usd:.2f} (fee ${fee:.4f})! Profit: ${profit:.4f} 🤑"
        debug_logs.append(log)
        logging.info(log)
    total_trades += 1
    trade_logs.append(log)
    if len(trade_logs) > 10:
        trade_logs.pop(0)

# Section: Initial Buys
def initial_buys():
    for coin in coins:
        symbol = coin['symbol']
        retry_count = 0
        while retry_count < MAX_INITIAL_RETRIES and prices[symbol] == 0.0:
            logging.debug(f"Waiting for {symbol} price data... retry {retry_count + 1}")
            time.sleep(2)
            retry_count += 1
        if prices[symbol] > 0 and balances['USDT'] >= INITIAL_BUY_USD:
            execute_trade(symbol, 'buy', INITIAL_BUY_USD)
            last_trade_times[symbol] = time.time()
        else:
            log = f"Initial buy for {symbol} failed - no price or insufficient USDT after retries!"
            trade_logs.append(log)
            logging.warning(log)
initial_thread = threading.Thread(target=initial_buys)
initial_thread.daemon = True
initial_thread.start()

# Section: Reset Simulation
def reset_simulation():
    global balances, invested, coin_profits, total_profit, total_trades, trade_logs, last_trade_times
    balances = {'USDT': INITIAL_BALANCE}
    for coin in coins:
        symbol = coin['symbol']
        balances[symbol] = 0.0
        invested[symbol] = 0.0
        coin_profits[symbol] = 0.0
        last_trade_times[symbol] = 0
    total_profit = 0.0
    total_trades = 0
    trade_logs = []
    initial_buys()
    log = "Simulation reset! Fresh start. 🔄"
    trade_logs.append(log)
    logging.info(log)

# Section: Trading Loop
def trading_loop():
    while True:
        current_time = time.time()
        for coin in coins:
            symbol = coin['symbol']
            price = prices.get(symbol, 0.0)
            ema = emas.get(symbol, 0.0)
            if price == 0.0 or ema == 0.0:
                logging.debug(f"Skipping {symbol} - price or EMA not ready")
                continue
            if current_time - last_trade_times.get(symbol, 0) < COOLDOWN_SEC:
                logging.debug(f"Skipping {symbol} - in cooldown (last trade: {last_trade_times[symbol]})")
                continue
            deviation = (price - ema) / ema * 100 if ema > 0 else 0.0
            deviations[symbol] = deviation
            logging.debug(f"{symbol} deviation: {deviation:.2f}%")
            if deviation <= -THRESHOLD_PCT_BUY + THRESHOLD_TOLERANCE and balances['USDT'] >= TRADE_AMOUNT_USD:
                execute_trade(symbol, 'buy', TRADE_AMOUNT_USD)
                last_trade_times[symbol] = current_time
            sell_threshold = THRESHOLD_PCT_SELL_HIGH if invested[symbol] > 0 and coin_profits[symbol] > invested[symbol] * PROFIT_HIGH_MARK else THRESHOLD_PCT_SELL_BASE
            if deviation >= sell_threshold - THRESHOLD_TOLERANCE and balances[symbol] > (TRADE_AMOUNT_USD / price):
                execute_trade(symbol, 'sell', TRADE_AMOUNT_USD)
                last_trade_times[symbol] = current_time
        time.sleep(0.1)  # Faster loop for high trade volume
trade_thread = threading.Thread(target=trading_loop)
trade_thread.daemon = True
trade_thread.start()

# Section: Title Screen
def title_screen(stdscr):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_CYAN, -1)
    height, width = stdscr.getmaxyx()
    title_lines = [
        "YYYYY OOO U U RRRR ",
        " Y O O U U R R ",
        " Y O O U U RRRR ",
        " Y O O U U R R ",
        " Y OOO UUU R RR ",
        "M O M M A ' S B E S T",
        "M E M E T R A D E R"
    ]
    powered = "Powered by Grok 4 by xAI"
    ascii_art = [
        " /\\_/\\ ",
        " ( o.o )",
        " > ^ < ",
        " MEME! "
    ]
    stdscr.clear()
    stdscr.border(curses.ACS_VLINE, curses.ACS_VLINE, curses.ACS_HLINE, curses.ACS_HLINE,
                  curses.ACS_ULCORNER, curses.ACS_URCORNER, curses.ACS_LLCORNER, curses.ACS_LRCORNER)
    start_row = height//2 - len(title_lines)//2 - 3
    for i, line in enumerate(title_lines):
        stdscr.addstr(start_row + i, (width - len(line)) // 2, line, curses.A_BOLD | curses.color_pair(1))
    stdscr.addstr(start_row + len(title_lines) + 1, (width - len(powered)) // 2, powered, curses.A_UNDERLINE | curses.color_pair(2))
    for i, line in enumerate(ascii_art):
        stdscr.addstr(start_row + len(title_lines) + 3 + i, (width - len(line)) // 2, line, curses.color_pair(2))
    stdscr.addstr(height - 2, (width - 20) // 2, "Press any key to start...", curses.A_BLINK)
    stdscr.refresh()
    stdscr.getch()

# Section: Dashboard
def dashboard(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(1000)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    instructions = "Automated trading running | r=reset | q=quit"
    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        title = "Your Momma's Best Meme Trader Powered by Grok 4 by xAI"
        stdscr.addstr(0, max(0, (width - len(title)) // 2), title, curses.A_BOLD | curses.color_pair(1))
        stdscr.hline(1, 0, curses.ACS_HLINE, width)
        stdscr.addstr(2, PADDING, instructions[:width-1 - PADDING], curses.color_pair(3))
        prices_start_row = 4
        stdscr.addstr(prices_start_row, PADDING, "Live Prices (Top Meme Coins by Volume on KuCoin):")
        row = prices_start_row + 2
        all_positive = True
        total_value = balances['USDT']
        for idx, coin in enumerate(coins, 1):
            symbol = coin['symbol']
            price = prices.get(symbol, 0.0)
            change = changes.get(symbol, 0.0)
            dev = deviations.get(symbol, 0.0)
            if change < 0:
                all_positive = False
            color = 1 if change > 0 else (2 if change < 0 else 5)
            price_str = f"{price:.10f}" if price < 1 else f"{price:.2f}"
            bal = balances.get(symbol, 0.0)
            inv = invested.get(symbol, 0.0)
            prof = coin_profits.get(symbol, 0.0)
            total_value += bal * price if price > 0 else bal * emas[symbol] if emas[symbol] > 0 else 0.0
            display_str = f"{idx}. {coin['name']} ({symbol}): ${price_str} ({change:+.2f}%) | Dev: {dev:+.2f}% | Bal: {bal:.4f} | Inv: ${inv:.2f} | Prof: ${prof:.2f}"
            stdscr.addstr(row, PADDING, display_str[:width-1 - PADDING], curses.color_pair(color))
            row += 1
        stdscr.hline(prices_start_row - 1, 0, curses.ACS_HLINE, width)
        stdscr.hline(row, 0, curses.ACS_HLINE, width)
        for r in range(prices_start_row - 1, row + 1):
            stdscr.addch(r, 0, curses.ACS_VLINE)
            stdscr.addch(r, width - 1, curses.ACS_VLINE)
        trades_start_row = row + 2
        stdscr.addstr(trades_start_row, PADDING, f"Trades (USDT: ${balances['USDT']:.2f} | Total Value: ${total_value:.2f} | Total Profit: ${total_profit:.2f} | Trades: {total_trades}):")
        row = trades_start_row + 2
        for log in trade_logs:
            stdscr.addstr(row, PADDING, log[:width-1 - PADDING], curses.color_pair(3))
            row += 1
            if row >= height - 2:
                break
        stdscr.hline(trades_start_row - 1, 0, curses.ACS_HLINE, width)
        stdscr.hline(row, 0, curses.ACS_HLINE, width)
        for r in range(trades_start_row - 1, row + 1):
            stdscr.addch(r, 0, curses.ACS_VLINE)
            stdscr.addch(r, width - 1, curses.ACS_VLINE)
        if all_positive and total_profit > 0 and row < height - 1:
            stdscr.addstr(row + 1, (width - 20) // 2, "To the Moon! 🌕🚀", curses.A_BOLD | curses.color_pair(1))
        stdscr.refresh()
        key = stdscr.getch()
        if key != -1 and chr(key).lower() in ['q', 'r']:
            debug_logs.append(f"Key pressed: {chr(key)}")
            if key == ord('q'):
                break
            elif key == ord('r'):
                reset_simulation()

# Section: Main Entry
def main(stdscr):
    title_screen(stdscr)
    dashboard(stdscr)
curses.wrapper(main)
print("\nDebug Logs:")
for dlog in debug_logs:
    print(dlog)
print(f"Bot exited. Total trades: {total_trades}, Profit: ${total_profit:.2f}. Fun sim – trade responsibly!")