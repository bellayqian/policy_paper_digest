"""
Daily Academic Paper Monitor
Monitors arXiv + journal RSS feeds, summarizes with Claude, sends Gmail digest.
"""

import os
import json
import smtplib
import feedparser
import anthropic
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIG — edit these to match your interests
# ─────────────────────────────────────────────
ARXIV_CATEGORIES = [
    "econ.GN",       # General Economics (health policy papers land here)
    "stat.AP",       # Applied Statistics (CMS/EHR methods)
    "cs.CY",         # Computers & Society (health IT / EHR)
    "q-bio.PE",      # Populations & Evolution (public health)
]

ARXIV_KEYWORDS = [
    "electronic health record", "EHR", "Medicare", "Medicaid", "CMS",
    "health policy", "public policy", "claims data", "administrative data",
    "insurance", "hospital", "patient outcome", "health care utilization",
    "social determinants", "Affordable Care Act", "ACA", "Medicaid expansion",
    "value-based care", "readmission", "mortality", "healthcare cost",
    "SDOH", "population health", "health disparities", "observational study",
]

JOURNAL_RSS_FEEDS = {
    "NEJM":                  "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss",
    "JAMA":                  "https://jamanetwork.com/rss/site_3/67.xml",
    "JAMA Internal Medicine":"https://jamanetwork.com/rss/site_3/73.xml",
    "JAMA Health Forum":     "https://jamanetwork.com/rss/site_3/157.xml",
    "Health Affairs":        "https://www.healthaffairs.org/rss/site_19/40.xml",
    "AJPH":                  "https://ajph.aphapublications.org/action/showFeed?type=etoc&feed=rss&jc=ajph",
    "Annals of Internal Med":"https://www.acpjournals.org/action/showFeed?type=etoc&feed=rss&jc=aim",
    "BMJ":                   "https://www.bmj.com/rss/current.xml",
    "Lancet":                "https://www.thelancet.com/rssfeed/lancet_current.xml",
    "JAGS":                  "https://agsjournals.onlinelibrary.wiley.com/feed/15325415/most-recent",
    "Medical Care":          "https://journals.lww.com/lww-medicalcare/_layouts/15/oaks.journals/feed.aspx?FeedType=MostPopularArticles",
    "Health Services Research":"https://onlinelibrary.wiley.com/action/showFeed?jc=14756773&type=etoc&feed=rss",
}

# How many papers max per source per day
MAX_ARXIV_PAPERS   = 8
MAX_JOURNAL_PAPERS = 3   # per journal

# Gmail settings — populated from env vars / GitHub Secrets
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
RECIPIENT      = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ─────────────────────────────────────────────
# ARXIV FETCHER
# ─────────────────────────────────────────────
def fetch_arxiv_papers():
    """Pull yesterday's papers matching keywords from arXiv categories."""
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    today     = datetime.date.today().strftime("%Y%m%d")

    cat_query = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    kw_query  = " OR ".join(f'ti:"{k}" OR abs:"{k}"' for k in ARXIV_KEYWORDS[:10])  # URL-safe subset
    query = f"({cat_query}) AND ({kw_query}) AND submittedDate:[{yesterday}0000 TO {today}2359]"

    url = (
        "https://export.arxiv.org/api/query?"
        f"search_query={urllib.parse.quote(query)}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={MAX_ARXIV_PAPERS * 3}"   # fetch more, then keyword-filter
    )

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
    except Exception as e:
        print(f"arXiv fetch error: {e}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("atom:entry", ns):
        title    = entry.findtext("atom:title",   "", ns).strip().replace("\n", " ")
        abstract = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")
        link     = entry.findtext("atom:id",      "", ns).strip()
        authors  = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]

        # Keyword relevance filter
        combined = (title + " " + abstract).lower()
        if not any(kw.lower() in combined for kw in ARXIV_KEYWORDS):
            continue

        papers.append({
            "source":   "arXiv",
            "title":    title,
            "abstract": abstract[:2000],
            "url":      link,
            "authors":  ", ".join(authors[:5]),
        })
        if len(papers) >= MAX_ARXIV_PAPERS:
            break

    print(f"arXiv: found {len(papers)} relevant papers")
    return papers


# ─────────────────────────────────────────────
# JOURNAL RSS FETCHER
# ─────────────────────────────────────────────
def fetch_journal_papers():
    """Pull recent papers from journal RSS feeds, filter by keywords."""
    all_papers = []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=30)

    for journal, rss_url in JOURNAL_RSS_FEEDS.items():
        try:
            feed = feedparser.parse(rss_url)
            count = 0
            for entry in feed.entries:
                if count >= MAX_JOURNAL_PAPERS:
                    break

                # Date filter — skip if older than ~30 hours
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    pub_dt = datetime.datetime(*pub[:6], tzinfo=datetime.timezone.utc)
                    if pub_dt < cutoff:
                        continue

                title    = entry.get("title", "").strip()
                abstract = entry.get("summary", entry.get("description", "")).strip()[:2000]
                url      = entry.get("link", "")
                authors  = entry.get("author", "")

                # Keyword filter
                combined = (title + " " + abstract).lower()
                if not any(kw.lower() in combined for kw in ARXIV_KEYWORDS):
                    continue

                all_papers.append({
                    "source":   journal,
                    "title":    title,
                    "abstract": abstract,
                    "url":      url,
                    "authors":  authors,
                })
                count += 1

            print(f"{journal}: found {count} relevant papers")
        except Exception as e:
            print(f"{journal} RSS error: {e}")

    return all_papers


# ─────────────────────────────────────────────
# CLAUDE SUMMARIZER
# ─────────────────────────────────────────────
def summarize_paper(client, paper):
    """Call Claude to produce structured bullet-point summary."""
    prompt = f"""You are a health policy and health services research expert reviewer.

Analyze this paper and provide a structured summary:

Title: {paper['title']}
Source: {paper['source']}
Authors: {paper['authors']}
Abstract: {paper['abstract']}

Respond with EXACTLY this format (use the headers as written):

📋 ONE-LINER
[One sentence: what the paper does and the main finding]

🔬 STUDY DESIGN
[Bullet: study type — RCT, cohort, difference-in-differences, cross-sectional, etc.]

🗃️ DATA & DATASET
[Bullet: exact dataset name, sample size if mentioned, time period, geography]

⚙️ KEY METHOD
[Bullet: main analytical method — IV, PSM, DiD, regression discontinuity, ML approach, etc.]

📊 MAIN FINDINGS
[2-3 bullets: specific results with numbers/effect sizes where available]

🏅 WHY TOP JOURNAL?
[2 bullets: what makes this novel or impactful enough for {paper['source']} — be specific and critical]

⚠️ LIMITATION TO WATCH
[1 bullet: the most important caveat]

Keep each bullet concise (1-2 sentences max). Be direct and analytical."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        print(f"Claude summarization error for '{paper['title']}': {e}")
        return "⚠️ Summary unavailable."


# ─────────────────────────────────────────────
# EMAIL BUILDER
# ─────────────────────────────────────────────
def build_email_html(papers_with_summaries):
    today_str = datetime.date.today().strftime("%B %d, %Y")
    total = len(papers_with_summaries)

    # Group by source
    by_source = {}
    for p, summary in papers_with_summaries:
        src = p["source"]
        by_source.setdefault(src, []).append((p, summary))

    cards_html = ""
    for source, items in sorted(by_source.items()):
        cards_html += f"""
        <div style="margin-bottom:8px;">
          <span style="background:#1a3a5c;color:#fff;padding:4px 12px;border-radius:20px;
                       font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;">
            {source}
          </span>
        </div>"""
        for paper, summary in items:
            # Convert markdown-ish bullets to HTML
            summary_html = ""
            for line in summary.strip().split("\n"):
                line = line.strip()
                if not line:
                    summary_html += "<br>"
                elif line.startswith(("📋","🔬","🗃️","⚙️","📊","🏅","⚠️")):
                    summary_html += f'<p style="margin:12px 0 4px;font-weight:700;color:#1a3a5c;">{line}</p>'
                elif line.startswith("- ") or line.startswith("• ") or line.startswith("["):
                    text = line.lstrip("-•[ ]")
                    summary_html += f'<p style="margin:2px 0 2px 16px;color:#333;">• {text}</p>'
                else:
                    summary_html += f'<p style="margin:2px 0;color:#444;">{line}</p>'

            cards_html += f"""
            <div style="background:#fff;border:1px solid #e0e8f0;border-left:4px solid #2563eb;
                        border-radius:8px;padding:20px 24px;margin:12px 0 24px;">
              <h3 style="margin:0 0 6px;font-size:17px;line-height:1.4;color:#111;">
                <a href="{paper['url']}" style="color:#1a3a5c;text-decoration:none;">{paper['title']}</a>
              </h3>
              <p style="margin:0 0 14px;font-size:12px;color:#666;">{paper['authors']}</p>
              <div style="font-size:14px;line-height:1.6;">{summary_html}</div>
              <a href="{paper['url']}" style="display:inline-block;margin-top:14px;padding:7px 18px;
                 background:#2563eb;color:#fff;border-radius:6px;font-size:13px;
                 text-decoration:none;font-weight:600;">Read Paper →</a>
            </div>"""

    if not cards_html:
        cards_html = """<p style="color:#555;text-align:center;padding:40px;">
            No new papers matched your keywords today. Check back tomorrow!</p>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:'Georgia',serif;">
  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1a3a5c 0%,#2563eb 100%);
                border-radius:12px;padding:28px 32px;margin-bottom:24px;text-align:center;">
      <p style="margin:0 0 4px;color:#93c5fd;font-size:12px;letter-spacing:2px;text-transform:uppercase;">
        Daily Research Digest
      </p>
      <h1 style="margin:0 0 8px;color:#fff;font-size:26px;font-weight:700;">{today_str}</h1>
      <p style="margin:0;color:#bfdbfe;font-size:14px;">
        {total} new paper{"s" if total != 1 else ""} across EHR · CMS · Health Policy · Public Policy
      </p>
    </div>

    <!-- Papers -->
    {cards_html}

    <!-- Footer -->
    <div style="text-align:center;padding:16px;color:#94a3b8;font-size:12px;">
      Summaries generated by Claude · 
      <a href="https://github.com" style="color:#94a3b8;">Powered by GitHub Actions</a>
    </div>
  </div>
</body>
</html>"""


def send_email(html_body, paper_count):
    today_str = datetime.date.today().strftime("%b %d")
    subject = f"📄 Research Digest {today_str} — {paper_count} new paper{'s' if paper_count != 1 else ''}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT

    # Plain text fallback
    plain = f"Daily Research Digest — {paper_count} new papers. Open HTML version to read."
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    print(f"✅ Email sent to {RECIPIENT}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"Paper Monitor — {datetime.date.today()}")
    print(f"{'='*50}\n")

    # Validate env vars
    missing = [v for v in ["ANTHROPIC_API_KEY","GMAIL_USER","GMAIL_APP_PASS"] if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 1. Fetch papers
    print("📡 Fetching arXiv papers...")
    arxiv_papers = fetch_arxiv_papers()

    print("\n📰 Fetching journal RSS feeds...")
    journal_papers = fetch_journal_papers()

    all_papers = arxiv_papers + journal_papers
    print(f"\n✅ Total: {len(all_papers)} papers to summarize\n")

    if not all_papers:
        print("No papers found today. Sending empty digest.")
        html = build_email_html([])
        send_email(html, 0)
        return

    # 2. Summarize with Claude
    papers_with_summaries = []
    for i, paper in enumerate(all_papers, 1):
        print(f"🤖 Summarizing [{i}/{len(all_papers)}]: {paper['title'][:70]}...")
        summary = summarize_paper(client, paper)
        papers_with_summaries.append((paper, summary))

    # 3. Build and send email
    print("\n📧 Building and sending email digest...")
    html = build_email_html(papers_with_summaries)
    send_email(html, len(papers_with_summaries))
    print("\n🎉 Done!")


if __name__ == "__main__":
    import urllib.parse  # ensure available
    main()
