# CAM PLAYS Dashboard

Real-time Camarilla Pivot Plays dashboard powered by TradingView webhook alerts.

## How it works

```
TradingView Chart Indicator → Webhook Alert → This Server → Dashboard
```

The server receives webhook JSON from TradingView alerts, maintains the current state of each instrument's plays, and serves a dashboard that polls for updates every 2 seconds.

## Deploy to Railway

### 1. Create GitHub Repository

1. Go to https://github.com/new
2. Repository name: `cam-plays-dashboard`
3. Set to **Private**
4. Click **Create repository**
5. On the next page, click **"uploading an existing file"**
6. Upload ALL files from this project (drag the entire folder contents)
7. Click **Commit changes**

### 2. Deploy on Railway

1. Go to https://railway.com/dashboard
2. Click **New Project**
3. Choose **Deploy from GitHub Repo**
4. Select `cam-plays-dashboard`
5. Railway will auto-detect Python and start building
6. Once deployed, go to **Settings** → **Networking** → **Generate Domain**
7. Copy your public URL (e.g., `cam-plays-dashboard-production.up.railway.app`)

### 3. Test the deployment

Visit your Railway URL in a browser — you should see the dashboard with your default instruments (all idle).

Test the webhook with this URL in your browser:
```
https://YOUR-RAILWAY-URL/health
```
Should return: `{"status": "healthy", "instruments": 10}`

### 4. Connect TradingView

In TradingView, on each chart with the BruzX indicator:

1. Set an alert on the indicator
2. In the alert dialog, check **Webhook URL**
3. Paste: `https://YOUR-RAILWAY-URL/webhook`
4. The alert message is already formatted as JSON by the indicator's webhook mode

## Webhook Format

The server accepts POST requests to `/webhook` with this JSON:

```json
{
    "ticker": "ES1!",
    "event": "entry",
    "play": "HA",
    "time": "2026-04-07 09:42:00"
}
```

### Event types:
- `entry` — Play entry triggered (requires `play`)
- `candidate` — Play became a candidate (requires `play`)
- `candidate_remove` — Play no longer a candidate (requires `play`)
- `exit` — Play exited (requires `play`)
- `context` — Update range/width/data source (include `range`, `width`, `dataSource`)
- `session_reset` — Clear all plays for new session

### Testing with curl:

```bash
# Send a context update
curl -X POST https://YOUR-RAILWAY-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"ticker":"ES1!","event":"context","range":"Higher","width":"Wide","dataSource":"RTH"}'

# Send a candidate
curl -X POST https://YOUR-RAILWAY-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"ticker":"ES1!","event":"candidate","play":"HA","time":"2026-04-07 09:35:00"}'

# Trigger an entry
curl -X POST https://YOUR-RAILWAY-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"ticker":"ES1!","event":"entry","play":"HA","time":"2026-04-07 09:42:00"}'

# Exit a play
curl -X POST https://YOUR-RAILWAY-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"ticker":"ES1!","event":"exit","play":"HA","time":"2026-04-07 10:15:00"}'
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard page |
| `/webhook` | POST | Receive TradingView alerts |
| `/api/state` | GET | Current state of all instruments |
| `/api/instruments` | POST | Add a new instrument |
| `/api/reset` | POST | Reset all instruments |
| `/health` | GET | Health check |

## File Structure

```
cam-plays-dashboard/
├── server/
│   └── app.py          # Python server (Flask)
├── static/
│   └── index.html      # Dashboard frontend
├── requirements.txt    # Python dependencies
├── Procfile            # Railway process command
├── railway.toml        # Railway configuration
├── .python-version     # Python version
└── README.md           # This file
```
