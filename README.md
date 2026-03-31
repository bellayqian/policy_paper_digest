# 📄 Daily Research Paper Digest

Automatically monitors arXiv + top health/policy journals every morning, summarizes new papers with Claude AI, and emails you a beautiful HTML digest.

**Covers your interests:** EHR · CMS Medicare/Medicaid · Health Policy · Public Policy

---

## What you get in each email

For every relevant paper:
- 📋 **One-liner** — what the paper does + main finding
- 🔬 **Study design** — RCT, cohort, DiD, etc.
- 🗃️ **Dataset** — exact name, sample size, time period
- ⚙️ **Method** — IV, PSM, ML approach, etc.
- 📊 **Main findings** — specific results with numbers
- 🏅 **Why top journal?** — critical novelty assessment
- ⚠️ **Key limitation** — most important caveat

---

## Quick Setup (30 minutes total)

### Step 1 — Create a private GitHub repo

1. Go to [github.com/new](https://github.com/new)
2. Name it `paper-digest` (or anything you like)
3. Set it to **Private**
4. Click **Create repository**

### Step 2 — Upload the files

Upload these 3 files to your repo (drag & drop on GitHub works):

```
paper_monitor.py          ← main script
requirements.txt          ← Python dependencies
.github/
  workflows/
    daily_digest.yml      ← GitHub Actions schedule
```

> **Important for the workflow file:** GitHub needs the folder structure `.github/workflows/`.
> On GitHub, click "Add file → Create new file", type `.github/workflows/daily_digest.yml` as the filename, paste the content.

### Step 3 — Get a Gmail App Password

> Regular Gmail password won't work — you need an App Password.

1. Go to your Google Account → [myaccount.google.com](https://myaccount.google.com)
2. Security → **2-Step Verification** (must be ON first)
3. Security → **App Passwords** (search for it if hidden)
4. Select app: **Mail** | Select device: **Other** → type "Paper Digest"
5. Copy the 16-character password shown (e.g. `abcd efgh ijkl mnop`)

### Step 4 — Get your Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. API Keys → **Create Key**
3. Copy it (starts with `sk-ant-...`)

### Step 5 — Add Secrets to GitHub

In your GitHub repo:
1. Click **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add these 4 secrets:

| Secret Name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-...`) |
| `GMAIL_USER` | Your Gmail address (`you@gmail.com`) |
| `GMAIL_APP_PASS` | The 16-char App Password from Step 3 |
| `RECIPIENT_EMAIL` | Where to send digests (can be same as Gmail) |

### Step 6 — Test it manually

1. In your repo, click the **Actions** tab
2. Click **Daily Paper Digest** in the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Watch the logs — should complete in ~2-3 minutes
5. Check your inbox!

### Step 7 — It runs automatically every morning

The schedule in `daily_digest.yml` runs at **8:00 AM Eastern** daily.
To change the time, edit the cron expression:

```yaml
- cron: '0 13 * * *'   # 13:00 UTC = 8:00 AM ET
- cron: '0 12 * * *'   # 12:00 UTC = 7:00 AM ET
- cron: '30 14 * * *'  # 14:30 UTC = 9:30 AM ET
```

Use [crontab.guru](https://crontab.guru) to build any schedule you want.

---

## Customizing Your Keywords & Journals

Open `paper_monitor.py` and edit these sections at the top:

### Add/remove keywords
```python
ARXIV_KEYWORDS = [
    "electronic health record", "EHR", "Medicare", ...
    # Add your terms here
]
```

### Add/remove journals
```python
JOURNAL_RSS_FEEDS = {
    "NEJM": "https://...",
    # Add new journals here
}
```
To find an RSS feed for any journal, Google: `[journal name] RSS feed`

### Tune paper volume
```python
MAX_ARXIV_PAPERS   = 8    # arXiv papers per day
MAX_JOURNAL_PAPERS = 3    # papers per journal per day
```

---

## Costs

| Component | Cost |
|-----------|------|
| GitHub Actions | Free (uses ~2 min/day, limit is 2,000/month) |
| arXiv API | Free |
| Journal RSS feeds | Free |
| Claude API | ~$0.01–0.05/day depending on paper count |

On Anthropic's Pro plan, API costs are billed separately via [console.anthropic.com](https://console.anthropic.com).

---

## Troubleshooting

**No email received**
- Check the Actions tab for error logs
- Verify all 4 GitHub Secrets are set correctly
- Make sure Gmail 2FA is on and App Password is 16 chars (no spaces)

**No papers found**
- Try running on a weekday (journals publish Mon–Fri)
- Widen your keywords in `ARXIV_KEYWORDS`
- Lower `MAX_JOURNAL_PAPERS` date filter (currently 30 hours lookback)

**Claude API error**
- Check your API key at console.anthropic.com
- Ensure you have billing set up (even with Pro plan, API is separate)
