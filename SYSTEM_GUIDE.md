# Polymarket Copy-Trading Bot - System Guide

## Overview

This is a **paper trading** bot that simulates copying trades from successful Polymarket traders (primarily "Cigarettes"). It tracks what your portfolio would look like if you automatically copied their trades.

**Not a real trading bot** - no actual money is moved. It's a simulation for analysis.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Polymarket     │────▶│  Track Script   │────▶│  Grafana        │
│  APIs           │     │  (Railway)      │     │  Dashboard      │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌─────────────────┐
                        │  Trade Log      │
                        │  (JSON file)    │
                        └─────────────────┘
```

### Components

| Component | Purpose | Location |
|-----------|---------|----------|
| `track_cigarettes.py` | Main bot - monitors wallets, simulates trades | `scripts/` |
| Trade Log | Stores all simulated trades | `/app/data/cigarettes_trades.json` |
| State File | Persists trading_paused, daily values | `/app/data/tracker_state.json` |
| Grafana | Visual dashboard | Railway (separate service) |
| Prometheus | Metrics collection | Railway (separate service) |

## How It Works

### 1. Trade Detection
The bot monitors tracked wallets via Polymarket's Data API:
- Polls every few seconds for new trades
- Detects BUY/SELL activity from watched traders

### 2. Trade Copying (Simulated)
When a tracked trader makes a move:
```
Cigarettes buys $1000 of "Trump wins" at $0.60
    ↓
Bot simulates: Buy $100 (10% ratio) at $0.60
    ↓
Records trade in log, updates paper balance
```

### 3. Position Resolution
When markets close:
- Resolution checker runs every 5 minutes
- Checks Polymarket Gamma API for closed markets
- Creates SELL trades with final P&L (WIN at $1, LOSS at $0)

### 4. Risk Controls
- **Daily loss limit**: Pauses trading if down >10% for the day
- **Max exposure**: Can't exceed 100% of portfolio in positions
- **Auto-unpause**: Resumes if daily P&L recovers to break-even

## Key Files

```
poly/
├── scripts/
│   └── track_cigarettes.py    # Main bot (2800+ lines)
├── logs/
│   └── cigarettes_trades.json # Local trade log (outdated)
├── grafana/
│   └── provisioning/
│       └── dashboards/
│           └── copy-trading.json  # Dashboard config
├── Dockerfile                 # Container build
├── railway.json              # Railway deployment config
└── SYSTEM_GUIDE.md           # This file
```

## Deployment Workflow

### Prerequisites
- GitHub repo: `github.com:cammann44/poly.git`
- Railway CLI installed: `npm install -g @railway/cli`
- Railway project linked

### Making Changes

1. **Edit code locally**
   ```bash
   cd /home/ybrid22/projects/hybrid/poly
   # Make changes to scripts/track_cigarettes.py
   ```

2. **Test syntax**
   ```bash
   python3 -m py_compile scripts/track_cigarettes.py
   ```

3. **Commit changes**
   ```bash
   git add scripts/track_cigarettes.py
   git commit -m "Description of changes"
   ```

4. **Push to GitHub**
   ```bash
   git push origin main
   ```

5. **Deploy to Railway**
   ```bash
   railway up --detach
   ```
   This uploads code and triggers a rebuild/redeploy.

6. **Monitor deployment**
   ```bash
   railway logs
   ```

### Railway CLI Commands

| Command | Purpose |
|---------|---------|
| `railway status` | Check current project/service |
| `railway logs` | View live logs |
| `railway logs --tail 50` | Last 50 log lines |
| `railway up` | Deploy (waits for completion) |
| `railway up --detach` | Deploy (returns immediately) |

## API Endpoints

The bot exposes a REST API on port 8765:

### Read Endpoints (GET)

| Endpoint | Purpose |
|----------|---------|
| `/summary` | Portfolio overview (balance, P&L, ROI) |
| `/health` | System health check |
| `/trades` | Raw trade log |
| `/all` | All trades formatted for display |
| `/risk` | Risk status (paused, exposure) |
| `/missed` | Trades blocked by limits |
| `/wallets` | Per-trader breakdown |
| `/daily` | Daily P&L history |
| `/download` | CSV export |

### Action Endpoints (POST)

| Endpoint | Purpose |
|----------|---------|
| `/unpause` | Manually unpause trading |
| `/recalculate` | Rebuild portfolio from trade log |
| `/resolve` | Manually trigger resolution check |
| `/backfill-outcomes` | Fill missing market names/slugs |

### Example Usage

```bash
# Check summary
curl https://tracker-production-e869.up.railway.app/summary

# Unpause trading
curl -X POST https://tracker-production-e869.up.railway.app/unpause

# Recalculate (fix data issues)
curl -X POST https://tracker-production-e869.up.railway.app/recalculate
```

## Configuration

### Environment Variables (Railway)

| Variable | Purpose |
|----------|---------|
| `DISCORD_WEBHOOK_URL` | Optional: alerts for issues |
| `COLD_WALLET_ADDRESS` | Not used (paper trading) |
| `HOT_WALLET_PRIVATE_KEY` | Not used (paper trading) |

### Code Constants (track_cigarettes.py)

```python
STARTING_BALANCE = 75000      # Initial paper balance
COPY_RATIO = 0.1              # Copy 10% of trader's size
MAX_COPY_SIZE = 500           # Max $500 per trade
MIN_COPY_SIZE = 10            # Min $10 per trade
MAX_PORTFOLIO_EXPOSURE = 1.0  # 100% max in positions
DAILY_LOSS_LIMIT = 0.10       # Pause at 10% daily loss
```

## Grafana Dashboard

**URL**: https://grafana-production-8c2b.up.railway.app

Shows:
- Portfolio value, ROI, win rate
- Open positions with live P&L
- Trade history
- Per-trader performance
- Missed trades analysis

Data comes from the tracker's API endpoints via Infinity datasource.

## Common Issues & Fixes

### Numbers look wrong / jumping around
```bash
curl -X POST https://tracker-production-e869.up.railway.app/recalculate
```
This rebuilds all calculations from the trade log.

### Trading is paused but shouldn't be
```bash
curl -X POST https://tracker-production-e869.up.railway.app/unpause
```

### Service not responding
```bash
railway logs  # Check for errors
railway up --detach  # Redeploy
```

### Need to check current state
```bash
curl https://tracker-production-e869.up.railway.app/health | python3 -m json.tool
```

## Data Flow

```
1. Polymarket API → Bot detects trade
2. Bot calculates copy size (10% of original)
3. Trade logged to JSON file
4. Metrics updated (Prometheus)
5. Balance/positions recalculated
6. Grafana polls /summary, /all endpoints
7. Dashboard updates
```

## Tracked Wallets

Configured in `scripts/config/wallets.json`:
- cigarettes (primary)
- DrPufferfish
- gmanas
- kch123
- anon_whale

## Key Concepts

### Paper Balance
Starts at $75,000. Goes down when "buying", up when "selling".

### Exposure
Total $ amount in open positions (cost basis).

### Unrealised P&L
Current value of open positions minus cost. Changes with market prices.

### Realised P&L
Profit/loss from closed positions. Only changes when positions resolve.

### Portfolio Value
Balance + Exposure + Unrealised P&L

### ROI
(Portfolio Value - Starting Balance) / Starting Balance * 100
