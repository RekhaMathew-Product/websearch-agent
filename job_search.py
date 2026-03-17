import os
import json
import requests
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

JOB_TITLES = [
    "Project Manager",
    "Product Manager",
    "Programme Manager"
]

TARGET_COMPANIES = [
    "Fitbit",
    "Google Pixel Watch",
    "YouTube Health",
    "Google Health",
    "Strava",
    "Zoe",
    "FitXR",
    "WHOOP",
    "Holland & Barrett"
]

COMPANY_NAMES_FOR_FILTER = [
    "Fitbit", "Pixel Watch", "YouTube Health", "Google Health",
    "Health Connect", "Strava", "Zoe", "FitXR", "WHOOP", "Holland & Barrett"
]

ALERT_THRESHOLD = 7  # email alert if score >= this

CANDIDATE_PROFILE = """
Senior Project/Product/Programme Manager with experience in tech-enabled products and services.
Looking for roles specifically at health & fitness tech companies including:
Fitbit, Google Health, YouTube Health, Pixel Watch, Strava, Zoe, FitXR, WHOOP, Holland & Barrett.
Preferred locations: London or Remote UK.
Values: impact-driven work, modern tech stack, collaborative culture.
Seniority: mid to senior level (5+ years experience).
"""

# ─────────────────────────────────────────────
# SECRETS — loaded from GitHub Actions secrets
# ─────────────────────────────────────────────

SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")


# ─────────────────────────────────────────────
# STEP 1: SEARCH FOR JOBS (LAST 24 HOURS ONLY)
# ─────────────────────────────────────────────

def is_target_company(company_name):
    company_lower = company_name.lower()
    for target in COMPANY_NAMES_FOR_FILTER:
        if target.lower() in company_lower:
            return True
    return False


def is_within_24_hours(job):
    extensions = job.get("detected_extensions", {})
    posted_at = extensions.get("posted_at", "").lower()

    if not posted_at:
        return True

    recent_indicators = [
        "just now", "minute ago", "minutes ago",
        "hour ago", "hours ago", "today", "1 day ago"
    ]
    for indicator in recent_indicators:
        if indicator in posted_at:
            return True

    older_indicators = [
        "days ago", "week ago", "weeks ago",
        "month ago", "months ago"
    ]
    for indicator in older_indicators:
        if indicator in posted_at:
            return False

    return True


def search_jobs():
    all_jobs = []
    seen_keys = set()
    skipped_old = 0

    for title in JOB_TITLES:
        for company in TARGET_COMPANIES:
            query = f"{title} at {company} UK"
            print(f"🔍 Searching: {query}")

            params = {
                "engine": "google_jobs",
                "q": query,
                "location": "United Kingdom",
                "api_key": SERPAPI_KEY,
                "num": 10,
                "chips": "date_posted:today"
            }

            try:
                response = requests.get("https://serpapi.com/search", params=params)
                data = response.json()
                jobs = data.get("jobs_results", [])

                for job in jobs:
                    company_name = job.get("company_name", "")

                    if not is_target_company(company_name):
                        print(f"  ⏭️ Skipping {company_name} — not a target company")
                        continue

                    if not is_within_24_hours(job):
                        posted = job.get("detected_extensions", {}).get("posted_at", "unknown")
                        print(f"  ⏭️ Skipping old job: {job.get('title')} @ {company_name} — posted {posted}")
                        skipped_old += 1
                        continue

                    key = f"{job.get('title', '')}_{company_name}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    posted_at = job.get("detected_extensions", {}).get("posted_at", "Unknown")

                    all_jobs.append({
                        "title": job.get("title", ""),
                        "company": company_name,
                        "location": job.get("location", ""),
                        "description": job.get("description", "")[:2000],
                        "salary": extract_salary(job),
                        "url": extract_url(job),
                        "date_found": datetime.today().strftime("%Y-%m-%d"),
                        "posted_at": posted_at
                    })

            except Exception as e:
                print(f"⚠️ SerpAPI error for '{query}': {e}")

    print(f"✅ Found {len(all_jobs)} new jobs in last 24hrs ({skipped_old} older jobs skipped)")
    return all_jobs


def extract_salary(job):
    highlights = job.get("job_highlights", [])
    for h in highlights:
        if "Salary" in h.get("title", "") or "Pay" in h.get("title", ""):
            items = h.get("items", [])
            if items:
                return items[0]
    return "Not listed"


def extract_url(job):
    related = job.get("related_links", [])
    if related:
        return related[0].get("link", "")
    return job.get("share_link", "")


# ─────────────────────────────────────────────
# STEP 2: SCORE JOBS WITH CLAUDE AI
# ─────────────────────────────────────────────

def score_job(job):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""You are evaluating a job listing for a candidate with this profile:
{CANDIDATE_PROFILE}

Job listing:
Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
Salary: {job['salary']}
Description: {job['description']}

Score this job from 1-10 for how well it matches the candidate profile.
Also write a one-sentence reason.

Respond ONLY with valid JSON in this exact format:
{{"score": 8, "reason": "Strong match because..."}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        result = json.loads(response.content[0].text)
        return result.get("score", 0), result.get("reason", "")
    except Exception as e:
        print(f"⚠️ Claude scoring error: {e}")
        return 0, "Could not score"


# ─────────────────────────────────────────────
# STEP 3: SAVE TO NOTION DATABASE
# ─────────────────────────────────────────────

def save_to_notion(job, score, reason):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Job Title": {
                "title": [{"text": {"content": job["title"]}}]
            },
            "Company": {
                "rich_text": [{"text": {"content": job["company"]}}]
            },
            "Location": {
                "rich_text": [{"text": {"content": job["location"]}}]
            },
            "Salary": {
                "rich_text": [{"text": {"content": job["salary"]}}]
            },
            "Score": {
                "number": score
            },
            "Match Reason": {
                "rich_text": [{"text": {"content": reason}}]
            },
            "URL": {
                "url": job["url"] if job["url"] else None
            },
            "Date Found": {
                "date": {"start": job["date_found"]}
            },
            "Status": {
                "select": {"name": "New"}
            }
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            print(f"  ✅ Saved to Notion: {job['title']} @ {job['company']} (Score: {score})")
        else:
            print(f"  ⚠️ Notion error: {response.status_code} — {response.text[:200]}")
    except Exception as e:
        print(f"  ⚠️ Notion save error: {e}")


# ─────────────────────────────────────────────
# STEP 4: SEND EMAIL ALERT (7+ scores)
# ─────────────────────────────────────────────

def send_alert_email(high_scoring_jobs):
    if not high_scoring_jobs:
        return

    subject = f"🎯 {len(high_scoring_jobs)} Strong Job Match(es) — {datetime.today().strftime('%d %b %Y')}"

    body = f"Hi Rekha,\n\nYour daily job search found {len(high_scoring_jobs)} strong match(es) today:\n\n"
    body += "─" * 50 + "\n\n"

    for item in high_scoring_jobs:
        job, score, reason = item
        body += f"🎯 {job['title']}\n"
        body += f"   Company:  {job['company']}\n"
        body += f"   Location: {job['location']}\n"
        body += f"   Salary:   {job['salary']}\n"
        body += f"   Posted:   {job.get('posted_at', 'Unknown')}\n"
        body += f"   Score:    {score}/10\n"
        body += f"   Why:      {reason}\n"
        if job['url']:
            body += f"   Apply:    {job['url']}\n"
        body += "\n"

    body += "─" * 50 + "\n"
    body += "All results saved to your Notion database.\n\nGood luck! 🚀"

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"📧 Alert email sent — {len(high_scoring_jobs)} matches")
    except Exception as e:
        print(f"⚠️ Email error: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"🤖 Job Search Agent — {datetime.today().strftime('%d %b %Y')}")
    print(f"{'='*50}\n")

    jobs = search_jobs()

    if not jobs:
        print("No new jobs found in the last 24 hours.")
        return

    high_scoring_jobs = []
    print(f"\n📊 Scoring and saving {len(jobs)} jobs...\n")

    for job in jobs:
        print(f"  Scoring: {job['title']} @ {job['company']} (posted: {job.get('posted_at', 'unknown')})")
        score, reason = score_job(job)
        print(f"  📊 Score: {score}/10 — {reason}")
        save_to_notion(job, score, reason)

        if score >= ALERT_THRESHOLD:
            high_scoring_jobs.append((job, score, reason))

    print(f"\n📧 {len(high_scoring_jobs)} jobs scored {ALERT_THRESHOLD}+/10")
    send_alert_email(high_scoring_jobs)

    print(f"\n✅ Done! All results saved to Notion.\n")


if __name__ == "__main__":
    main()
