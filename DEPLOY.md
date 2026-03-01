# 🏠 Property Manager App — Deployment Guide

## What This App Does
- Receives tenant texts via Twilio
- AI drafts a reply automatically
- You review, edit, and approve before sending
- Detects repair requests and auto-matches vendors
- One tap to dispatch a vendor with full work order details

---

## STEP 1 — Upload to GitHub

1. Go to **github.com** → sign up free if needed
2. Click **"New repository"** → name it `property-manager`
3. Upload all 3 files: `app.py`, `requirements.txt`, `Procfile`

---

## STEP 2 — Deploy on Render.com

1. Go to **render.com** → sign up free
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub account → select `property-manager` repo
4. Set these settings:
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
5. Under **Environment Variables**, add:
   - `TWILIO_SID` = your Account SID
   - `TWILIO_TOKEN` = your Auth Token
   - `TWILIO_PHONE` = +18556389238
   - `ANTHROPIC_KEY` = your Anthropic API key
6. Click **Deploy**
7. Render gives you a URL like: `https://property-manager-xxxx.onrender.com`

---

## STEP 3 — Connect Twilio

1. Go to **console.twilio.com**
2. Phone Numbers → Manage → Active Numbers → click your number
3. Under **Messaging** → "When a message comes in":
   - Set to **Webhook**
   - URL: `https://YOUR-RENDER-URL.onrender.com/sms`
   - Method: **HTTP POST**
4. Save

---

## STEP 4 — Use the App

Bookmark your Render URL on your phone. That's your dashboard!

- **Inbox tab** — all tenant texts with AI-drafted replies
- **Tenants tab** — add tenants with their address/unit
- **Vendors tab** — add repair vendors with their specialty

When a repair request comes in, tap **"Dispatch Vendor"** and the app will auto-suggest the right vendor and generate the work order message.

---

## Enable Auto-Reply (when ready)

In `app.py`, find this section in the `/sms` route:
```python
# Auto-reply mode off by default — just acknowledge
resp = MessagingResponse()
# Uncomment below for auto-reply mode:
# resp.message(ai_draft)
```

Remove the `#` from `resp.message(ai_draft)` and redeploy.
