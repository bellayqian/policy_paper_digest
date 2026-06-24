"""
Daily Academic Paper Monitor
Monitors arXiv + journal RSS / PubMed feeds, summarizes with Claude, sends Gmail digest.
"""

import os
import re
import json
import time
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
    "stat.ME",       # Methodology -- THE home of new causal-inference estimators/theory
    "econ.EM",       # Econometrics -- DML, synthetic control, IV innovations land here
]

ARXIV_KEYWORDS = [
    # --- 1. 你原有的核心数据与政策底座 (保留并精简) ---
    "electronic health record", "EHR", "Medicare", "Medicaid", "CMS claims",
    "health policy", "administrative data", "social determinants of health", "SDOH",
    "value-based care", "health disparities",
    
    # --- 2. 你的特定靶点：心血管、食品安全与最新法案 (新增) ---
    "cardiovascular", "heart failure", "food insecurity", 
    "Inflation Reduction Act", "IRA health", "drug pricing",
    
    # --- 3. AI 与医疗交叉的“高光”词汇 (新增，用于寻找降维打击的灵感) ---
    "machine learning healthcare", "deep learning clinical", "predictive modeling",
    "risk stratification", "patient phenotyping", "clustering health", 
    "natural language processing clinical", "LLM healthcare", "artificial intelligence medicine",
    
    # --- 4. 顶尖的“统计/AI+政策”前沿方法论 (新增，哈佛 PhD 极度看重的硬核词汇) ---
    "causal inference", # 因果推断（连接AI与政策的绝对核心）
    "heterogeneous treatment effect", # 异质性治疗效果（用于发现政策对哪些特定人群最有效）
    "target trial emulation", # 目标试验模拟（目前顶级医学顶刊最爱用的观察性数据分析方法）
    "algorithmic fairness", # 算法公平性（医保数据叠加AI时，必讲的政治正确与科研热点）
    "missing data imputation", # 缺失值插补（完美契合我们之前说的预测隐形Food Insecurity）
    "reinforcement learning healthcare", # 强化学习在医疗决策中的应用

    # Updates Apr 22
    "insulin", "out-of-pocket", "drug spending", "prescription drug", 
    "Medicare Part D", "IRA cap", "cost sharing",

    # --- 5. Core RWE / causal inference METHODS terms (added Jun 20) ---
    "doubly robust", "double machine learning", "debiased machine learning",
    "marginal structural model", "synthetic control", "instrumental variable",
    "regression discontinuity", "transportability", "generalizability of trial results",
    "proximal causal inference", "targeted maximum likelihood", "TMLE",
    "negative control outcome", "real-world evidence", "external control arm",
    "g-computation", "g-formula", "semiparametric efficient",
]

JOURNAL_RSS_FEEDS = {
    "NEJM":                  "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss",
    # JAMA's "Current Issue" feed only includes articles once they're formally
    # slotted into a print issue -- per JAMA's own RSS docs, "New Online"/
    # "Online First" is the feed that updates daily with everything published
    # in the last 30 days. This is *why* the Myerson insulin/IRA paper (JAMA,
    # published online June 6, 2026) never showed up: it was Online First for
    # a while before any "Current Issue" ever picked it up, and by the time it
    # might have, it had likely already aged out of the 7-day lookback below.
    "JAMA (Online First)":   "https://jamanetwork.com/rss/site_3/onlineFirst_67.xml",
    "JAMA (Current Issue)":  "https://jamanetwork.com/rss/site_3/67.xml",
    # JAMA Internal Medicine -- previous URL (site_3/73.xml) used JAMA's own
    # site ID instead of JAMA IM's; corrected to site_15, with Online First added.
    "JAMA IM (Online First)":"https://jamanetwork.com/rss/site_15/onlineFirst_71.xml",
    "JAMA IM (Current Issue)":"https://jamanetwork.com/rss/site_15/71.xml",
    # JAMA Health Forum is online-only (no print issue), so it only has one
    # feed, "New Online". Previous URL (site_3/157.xml) was also wrong (used
    # JAMA's site ID); corrected to site_193.
    "JAMA Health Forum":     "https://jamanetwork.com/rss/site_193/185.xml",
    "Health Affairs":        "https://www.healthaffairs.org/rss/site_19/40.xml",
    "AJPH":                  "https://ajph.aphapublications.org/action/showFeed?type=etoc&feed=rss&jc=ajph",
    "Annals of Internal Med":"https://www.acpjournals.org/action/showFeed?type=etoc&feed=rss&jc=aim",
    "BMJ":                   "https://www.bmj.com/rss/current.xml",
    "Lancet":                "https://www.thelancet.com/rssfeed/lancet_current.xml",
    "JAGS":                  "https://agsjournals.onlinelibrary.wiley.com/feed/15325415/most-recent"
}

# ─────────────────────────────────────────────
# JOURNAL WHITELIST
# fetch_pubmed_papers()'s topic-keyword queries (e.g. "causal inference
# Medicare") do unrestricted PubMed text matching, so they will return a
# paper from ANY journal that happens to contain those words -- this is how
# "Fly", "Hernia", "Indian Pediatrics" etc. got into past digests. This
# whitelist is checked against PubMed's own Journal/Title field and only
# papers from a listed journal survive.
#
# It does NOT touch fetch_journal_papers() (the curated RSS feeds above) --
# those are already a trusted, hand-picked set.
#
# Matching is exact (after lowercasing, dropping a trailing "(...)"
# disambiguator like "(Cambridge, Mass.)", and dropping a leading "The "),
# except for JOURNAL_WHITELIST_PREFIXES, which is for journal *families*
# where every member is in-scope (currently just "jama", which covers JAMA,
# JAMA Internal Medicine, JAMA Health Forum, JAMA Cardiology, JAMA Oncology,
# etc. -- since the topic query already restricts to Medicare/causal-
# inference/etc. content, a JAMA-family hit is trusted without listing every
# spinoff by hand).
#
# To add a journal: add its PubMed "Journal/Title" name (lowercase) to
# JOURNAL_WHITELIST_EXACT. If unsure of the exact PubMed title, search the
# journal name at https://pubmed.ncbi.nlm.nih.gov and check the citation.
JOURNAL_WHITELIST_EXACT = {
    # -- core health policy / health services research --
    "health affairs",
    "health affairs scholar",
    "health economics",
    "health research policy and systems",
    "bmc health services research",

    # -- core biostatistics / causal-inference methods --
    # (these 8 are also explicitly journal-tag-harvested below in
    # fetch_pubmed_papers; listed here too so this filter is a no-op for them
    # rather than accidentally excluding them)
    "biometrics",
    "biostatistics",
    "statistics in medicine",
    "epidemiology",
    "american journal of epidemiology",
    "statistical methods in medical research",
    "pharmacoepidemiology and drug safety",
    "bmc medical research methodology",
    "journal of clinical epidemiology",

    # -- major outcomes journals matching your CV team / current projects --
    "journal of the american college of cardiology",
    "obstetrics and gynecology",
    "journal of the american geriatrics society",   # = "JAGS"
    "diabetes care",
    "bmc medicine",

    # -- already-trusted via the RSS feeds above; added here too so a
    # PubMed-path hit for the same journal isn't dropped for inconsistency --
    "new england journal of medicine",
    "lancet",
    "bmj",
    "annals of internal medicine",
    "american journal of public health",
}

JOURNAL_WHITELIST_PREFIXES = (
    "jama",   # covers the whole JAMA Network family
)

def _normalize_journal(name):
    name = name.lower().strip()
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()   # drop trailing "(...)"
    if name.startswith("the "):
        name = name[4:]
    return name

def is_whitelisted_journal(journal_name):
    norm = _normalize_journal(journal_name)
    if norm in JOURNAL_WHITELIST_EXACT:
        return True
    return any(norm.startswith(p) for p in JOURNAL_WHITELIST_PREFIXES)


# How many papers max per source per day
MAX_ARXIV_PAPERS   = 15
MAX_JOURNAL_PAPERS = 10   # per journal

# ─────────────────────────────────────────────
# NON-RESEARCH FILTERS
# Two filters for the same goal (drop comments/letters/corrections/reviews),
# using whichever signal each source actually gives us:
#
# 1. NON_RESEARCH_PREFIXES -- title-text matching, used by
#    fetch_journal_papers() (RSS feeds), since RSS entries are just a title
#    string with no structured "this is a Review" field.
#
# 2. NON_RESEARCH_PUBLICATION_TYPES -- used by fetch_pubmed_papers(), which
#    *does* get a structured <PublicationType> list per article from PubMed
#    (e.g. "Review", "Comment", "Published Erratum") -- a far more reliable
#    signal than scanning the title. This is matched against the lowercased
#    PublicationType text. Note this previously didn't exist at all for the
#    PubMed path, which is why corrections/reviews/comments pulled in via the
#    journal-tag-harvest queries (which grab *everything* recent from a
#    journal, not just topic matches) were getting through.
# ─────────────────────────────────────────────
NON_RESEARCH_PREFIXES = [
    "[comment]", "[editorial]", "[letter]", "[correction]", "[erratum]",
    "[response]", "[reply]", "[perspective]", "[viewpoint]", "[news]",
    "[review]", "[systematic review]",
    "thank you to", "in reply to", "author response",
]

NON_RESEARCH_PUBLICATION_TYPES = {
    "comment", "editorial", "letter", "news",
    "published erratum", "corrected and republished article",
    "retraction of publication", "retracted publication",
    "review", "systematic review",
    "biography", "interview", "congress", "newspaper article",
    "patient education handout", "video-audio media",
}

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
    yesterday = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y%m%d")
    today     = datetime.date.today().strftime("%Y%m%d")

    cat_query = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    # NOTE: previously this used ARXIV_KEYWORDS[:10], which happened to be the
    # first 10 *domain* terms (EHR, Medicare, SDOH, etc). That meant pure
    # methods papers (e.g. a new doubly-robust estimator validated on
    # simulated data, with no mention of "Medicare" anywhere) were never
    # even fetched from arXiv -- they'd fail this query before the keyword
    # filter below ever got a chance to look at them. Use the full list.
    kw_query  = " OR ".join(f'ti:"{k}" OR abs:"{k}"' for k in ARXIV_KEYWORDS)
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
# JOURNAL RSS / PubMed FETCHER
# ─────────────────────────────────────────────
def fetch_journal_papers():
    """Pull recent papers from journal RSS feeds, filter by keywords."""
    all_papers = []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)

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

                # Skip editorials, comments, letters, corrections, reviews
                if any(title.lower().startswith(p) or p in title.lower() for p in NON_RESEARCH_PREFIXES):
                    print(f"  ⏭ Skipping non-research: {title[:60]}")
                    continue

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

def fetch_pubmed_papers():
    """Search PubMed as a safety net for papers RSS might miss."""
    keywords = [
        # --- original policy-applied terms ---
        "Medicare insulin", "Medicare drug spending",
        "health policy Medicare", "Medicaid spending",

        # --- methods/causal-inference topic queries (added Jun 20) ---
        "causal inference Medicare", "target trial emulation",
        "doubly robust estimation health policy", "marginal structural model Medicare",
        "regression discontinuity health policy", "synthetic control method health",
        "double machine learning causal", "real-world evidence methods",
        "transportability generalizability trial", "heterogeneous treatment effect Medicare",

        # --- journal-tag harvests: these PubMed-indexed venues are where new
        # RWE/causal-inference METHODS get published, regardless of whether
        # the abstract happens to mention a policy keyword. [ta] = journal
        # title abbreviation, so each query below pulls everything recent
        # from that journal rather than filtering by topic. ---
        '"Stat Med"[ta]', '"Biometrics"[ta]', '"Biostatistics"[ta]',
        '"Am J Epidemiol"[ta]', '"Epidemiology"[ta]', '"Stat Methods Med Res"[ta]',
        '"Pharmacoepidemiol Drug Saf"[ta]', '"BMC Med Res Methodol"[ta]',
    ]

    all_papers = []
    cutoff_days = 8  # slightly wider than RSS window

    for kw in keywords:
        url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pubmed&term={urllib.parse.quote(kw)}"
            f"&reldate={cutoff_days}&datetype=pdat"
            f"&retmax=10&retmode=json"
        )
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                ids = json.loads(r.read())["esearchresult"]["idlist"]

            for pmid in ids:
                time.sleep(0.34)  # stay under NCBI's ~3 req/sec unauthenticated limit
                fetch_url = (
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                    f"?db=pubmed&id={pmid}&retmode=xml"
                )
                with urllib.request.urlopen(fetch_url, timeout=15) as r:
                    xml = r.read().decode()
                root = ET.fromstring(xml)
                article = root.find(".//Article")
                if not article:
                    continue
                title = article.findtext(".//ArticleTitle", "").strip()
                abstract = article.findtext(".//AbstractText", "").strip()[:2000]
                journal = article.findtext(".//Journal/Title", "PubMed").strip()

                # PubMed tags every article with one or more PublicationType
                # values (e.g. "Journal Article", "Review", "Comment"). This
                # is the gap that let corrections/reviews/comments through:
                # this filter didn't exist on the PubMed path at all before.
                pub_types = [pt.text.strip().lower() for pt in article.findall(".//PublicationType") if pt.text]
                non_research_hits = NON_RESEARCH_PUBLICATION_TYPES.intersection(pub_types)
                if non_research_hits:
                    print(f"  ⏭ Skipping non-research ({', '.join(non_research_hits)}): {title[:60]}")
                    continue

                if not is_whitelisted_journal(journal):
                    print(f"  ⏭ Skipping non-whitelisted journal: {journal}")
                    continue

                all_papers.append({
                    "source": journal,
                    "title": title,
                    "abstract": abstract,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "authors": "",
                })
            time.sleep(0.34)
        except Exception as e:
            print(f"PubMed error for '{kw}': {e}")
    
    print(f"PubMed: found {len(all_papers)} papers")
    return all_papers

# ─────────────────────────────────────────────
# CLAUDE SUMMARIZER
# ─────────────────────────────────────────────
def summarize_paper(client, paper):
    """Call Claude to produce structured bullet-point summary."""
    prompt = f"""You are a reviewer with dual expertise: health policy/health services research, AND biostatistics/causal-inference methodology. Analyze this paper and provide a structured summary.

If the abstract is brief or lacks specific numbers, infer likely methods from the journal/title/context
and clearly prefix those sentences with "Likely:" — do NOT use brackets like [INFERRED] or [comment].

If this is primarily a METHODS/THEORY paper (new estimator, identification strategy, algorithm) rather
than an applied empirical study, it's fine for STUDY DESIGN and DATA & DATASET to be brief or say
"Not applicable — methods paper" — don't force-fit a study design that isn't there. Put the real
substance of a methods paper in METHODOLOGICAL CONTRIBUTION instead.

STRICT FORMATTING RULES — failure to follow these will make the output unusable:
- Do NOT use markdown: no **bold**, no *italics*, no # headers, no --- dividers
- Do NOT use brackets [ ] anywhere in your response
- Write bullets as plain "• text" only — no nested bullets
- Numbers and percentages are fine; markdown symbols are not
- Be direct and concise — no filler phrases like "it is worth noting" or "it is important to highlight"

Title: {paper['title']}
Source: {paper['source']}
Abstract: {paper['abstract']}

Respond with EXACTLY this format:

📋 ONE-LINER
One sentence: what the paper does and the main finding.

🔬 STUDY DESIGN
- Study type: RCT / cohort / difference-in-differences / cross-sectional / methods-only / etc.

🗃️ DATA & DATASET
- Dataset name, sample size if mentioned, time period, geography. Or "Not applicable" / "simulation only".

⚙️ KEY METHOD
- Main analytical method: IV, PSM, DiD, regression discontinuity, ML, etc.

🧪 METHODOLOGICAL CONTRIBUTION
- What is actually new about the estimator/algorithm/identification strategy, and what existing approach
  it improves on (e.g. relaxes an assumption, reduces bias/variance, handles a new data structure).
- Whether code or a software package (R/Python/Stata) is mentioned as available.
- If this paper just applies established methods with no methodological novelty, say so plainly.

📊 MAIN FINDINGS
- Finding 1 with numbers if available.
- Finding 2 with numbers if available.
- Finding 3 if applicable.

🏅 WHY THIS MATTERS
- Novelty or impact point 1.
- Novelty or impact point 2.

⚠️ LIMITATION TO WATCH
- The single most important caveat, including key identifying assumptions if this is a causal-inference paper.

Each bullet must be 1-2 plain sentences. No markdown symbols anywhere."""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
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
                # Strip any markdown Claude sneaks in despite instructions
                line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)   # **bold**
                line = re.sub(r'\*(.+?)\*',   r'\1', line)      # *italic*
                line = re.sub(r'\[(.+?)\]',   r'\1', line)      # [brackets]
                line = re.sub(r'^#+\s*',       '',    line)      # # headers
                if not line:
                    summary_html += "<br>"
                elif line.startswith(("📋","🔬","🗃️","⚙️","🧪","📊","🏅","⚠️")):
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

    print("\n📰 Fetching journal RSS / PubMed feeds...")
    journal_papers = fetch_journal_papers()
    pubmed_papers = fetch_pubmed_papers()
    seen_titles = set()
    all_papers = []
    for p in arxiv_papers + journal_papers + pubmed_papers:
        key = p["title"].lower().strip()[:80]   # normalize for matching
        if key not in seen_titles:
            seen_titles.add(key)
            all_papers.append(p)

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
    main()
