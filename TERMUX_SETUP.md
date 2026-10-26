# Termux Setup — Signal Advisor v3

Mobile pe backtest chalane ke liye step-by-step guide.
Sab commands Termux app mein paste karein.

Tested on: Android 12+, Termux from F-Droid, Python 3.11

---

## 0. Pehle yeh check karein

- [ ] Termux **F-Droid** se install kiya (Play Store se NAHI) —
      https://f-droid.org/packages/com.termux/
- [ ] Phone mein **5 GB free storage**
- [ ] Charger connected (long commands ke liye)
- [ ] ZIP file phone ke **Downloads** folder mein hai

---

## 1. Termux update + packages (ek hi baar)

```bash
pkg update -y && pkg upgrade -y
pkg install -y python clang freetype libpng unzip git
```

⏱️ 5-10 minute. Ek baar hi karna hai.

---

## 2. Storage access

```
termux-setup-storage
```

Popup aayega → **Allow** dabayein.
Ab `~/storage/downloads/` = aapka Downloads folder.

---

## 3. Setup script chalao

ZIP ko Termux mein laayein:

```
cp ~/storage/downloads/signal-advisor-v3-termux.zip ~/
cd ~
unzip signal-advisor-v3-termux.zip
cd signal-advisor-v3-termux
bash setup-termux.sh
```

⏱️ 10-20 minute (pehli baar pip packages install ho rahe hain).
Ek baar ho gaya to dobara chalane ki zaroorat nahi.

Agar `matplotlib` install fail ho jaye — koi baat nahi, script baaki
sab install kar dega. Charts nahi banenge, lekin backtest chalega.

---

## 4. Data download (chhota scope)

```
python main.py download --days 60 --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 15m,1h,4h
```

⏱️ 2-4 minute. ~5 MB storage.

**Agar Bybit block hai** (403 error) to Binance try karein:

```
echo "DATA_EXCHANGE=binance_futures" > .env
python main.py download --days 60 --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 15m,1h,4h
```

---

## 5. Pehla backtest

```
python main.py backtest --mode day --days 60 --symbols BTC/USDT,ETH/USDT,SOL/USDT
```

⏱️ 3-5 minute mobile CPU pe.

Output aisa aayega:

```
=== Backtest: day ===
Report: data/reports/day_20261023_XXXXXX.html
trades=42  net=$-12.34  win=45.2%  sharpe=-0.12  maxDD=18.3%
```

---

## 6. Report kholein

```
termux-open data/reports/day_*.html
```

Phone ke browser mein khul jayegi.

---

## 7. Result kaise samjhein

Report ke "Summary" section mein yeh dekhein:

| Metric ↕▾ | Accha kya hai ↕▾ | Bura kya hai ↕▾ |
|---|---|---|
| −Trades | 30+ | <20 (insufficient data) |
| −Win rate | 45%+ | <40% |
| −Profit factor | **>1.3** | **<1.0** |
| −Net P&L $ | Positive | Negative |
| −Sharpe | **>0.5** | <0 |
| −Max drawdown | <20% | >30% |
⚙

**Agar profit factor <1.0 ya Sharpe negative hai** → strategy kaam
nahi kar rahi. Aage walk-forward chalane ka faida nahi.
Config mein parameters tune karein ya different mode try karein.

**Agar profit factor >1.3 aur Sharpe >0.5** → aage badhein:

```
python main.py walkforward --mode day --windows 2 --budget 20 --days 60
```

⏱️ 30-60 minute mobile pe. Charger lagayein, screen off na karein.

---

## Common errors

| Error ↕▾ | Fix ↕▾ |
|---|---|
| −`pyarrow` install fail | ZIP Termux version hai — ismein pyarrow nahi chahiye |
| −`matplotlib` install fail | `MATPLOTLIB_DISABLED=1 python main.py backtest ...` |
| −Bybit 403 | `.env` mein `DATA_EXCHANGE=binance_futures` |
| −`No such file: config.yaml` | `cd ~/signal-advisor-v3-termux` |
| −Memory error | `--symbols` kam karein (1 symbol try karein) |
| −Phone garam | `pkg install -y htop` phir `htop` se dekh sakte hain |
⚙

---

## Paper-live (optional, advanced)

Sirf agar backtest acha result de. Phone 24/7 on rakhna padega.

```
termux-wake-lock
python main.py paper --mode day --poll-seconds 120
```

Dashboard (alag Termux session mein):

```
cd ~/signal-advisor-v3-termux
python main.py dashboard --port 5000
```

Phone browser mein: `http://localhost:5000`

</BDS:create_file>

<BDS:create_file fileName="setup-termux.sh">

```bash
#!/data/data/com.termux/files/usr/bin/bash
# Signal Advisor v3 — one-shot Termux setup.
# Run inside the project directory: bash setup-termux.sh

set -e

echo "==> Updating Termux packages..."
pkg update -y || true
pkg upgrade -y || true

echo "==> Installing system packages..."
pkg install -y python clang freetype libpng unzip

echo "==> Upgrading pip..."
pip install --upgrade pip --no-cache-dir

echo "==> Installing Python dependencies..."
if ! pip install --no-cache-dir pandas numpy requests pyyaml python-dotenv pytest; then
    echo ""
    echo "!!! Core dependency install failed."
    echo "    Try: pkg install -y python-numpy python-pandas"
    echo "    Then re-run: bash setup-termux.sh"
    exit 1
fi

echo "==> Trying matplotlib (optional, for charts)..."
if pip install --no-cache-dir matplotlib; then
    echo "    matplotlib installed — charts enabled."
else
    echo "    matplotlib failed to install — charts will be disabled."
    echo "    Reports will still work (text-only). Set this before running backtest:"
    echo "      export MATPLOTLIB_DISABLED=1"
fi

echo ""
echo "==> Setup complete."
echo ""
echo "Next steps:"
echo "  1. Download data:"
echo "     python main.py download --days 60 --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 15m,1h,4h"
echo ""
echo "  2. Run backtest:"
echo "     python main.py backtest --mode day --days 60 --symbols BTC/USDT,ETH/USDT,SOL/USDT"
echo ""
echo "  3. Open report:"
echo "     termux-open data/reports/day_*.html"
echo ""

