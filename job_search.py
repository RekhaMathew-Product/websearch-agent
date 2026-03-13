import os
import json
import requests
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

# ─────────────────────────────────────────────
# CONFIGURATION — edit these to customise
# ─────────────────────────────────────────────

JOB_TITLES = [
    "Project Manager",
    "Product Manager",
    "Programme Manager"
]

LOCATIONS = ["London", "Remote UK"]

INDUSTRIES = ["Health", "Fitness", "Wellness", "Healthcare", "MedTech"]

ALERT_THRESHOLD = 7  # email alert if score >= this

CANDIDATE_PROFILE = """
Senior Project/Product/Programme Manager with experience in tech-enabled products and services.
Looking for roles in the Health, Fitness and Wellness industry.
Preferred locations: London or Remote UK.
Values: impact-driven work, modern tech stack, collaborative culture.
Seniority: mid to senior level (5+ years experience).
"""

# ─────────────────────────────────────────────
# SECRETS — loaded from environment variables
# (set these in GitHub Actions secrets)
# ─────────────────────────────────────────────

SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL")  # where to send alerts (can be same as gmail)
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")


# ─────────────────────────────────────────────
# STEP 1: SEARCH FOR JOBS VIA SERPAPI
# ─────────────────────────────────────────────

def search_jobs():
    all_jobs = []
    seen_titles = set()

    for title in JOB_TITLES:
        for location in LOCATIONS:
            query = f"{title} {' OR '.join(INDUSTRIES)} {location}"
            print(f"🔍 Searching: {query}")

            params = {
                "engine": "google_jobs",
                "q": query,
                "location": "United Kingdom",
                "api_key": SERPAPI_KEY,
                "num": 10
            }

            try:
                response = requests.get("https://serpapi.com/search", params=params)
                data = response.json()
                jobs = data.get("jobs_results", [])

                for job in jobs:
                    # deduplicate by title + company
                    key = f"{job.get('title', '')}_{job.get('company_name', '')}"
                    if key not in seen_titles:
                        seen_titles.add(key)
                        all_jobs.append({
                            "title": job.get("title", ""),
                            "company": job.get("company_name", ""),
                            "location": job.get("location", ""),
                            "description": job.get("description", "")[:1000],
                            "salary": extract_salary(job),
                            "url": extract_url(job),
                            "date_found": datetime.today().strftime("%Y-%m-%d")
                        })

            except Exception as e:
                print(f"⚠️ SerpAPI error for '{query}': {e}")

    print(f"✅ Found {len(all_jobs)} unique jobs")
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
            "Apply Link": {
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
# STEP 4: SEND EMAIL ALERT FOR STRONG MATCHES
# ─────────────────────────────────────────────

def send_alert_email(high_scoring_jobs):
    if not high_scoring_jobs:
        return

    subject = f"🎯 {len(high_scoring_jobs)} Strong Job Match(es) Found — {datetime.today().strftime('%d %b %Y')}"

    body = f"Hi,\n\nYour daily job search found {len(high_scoring_jobs)} strong match(es) today:\n\n"
    body += "─" * 50 + "\n\n"

    for item in high_scoring_jobs:
        job, score, reason = item
        body += f"🎯 {job['title']}\n"
        body += f"   Company:  {job['company']}\n"
        body += f"   Location: {job['location']}\n"
        body += f"   Salary:   {job['salary']}\n"
        body += f"   Score:    {score}/10\n"
        body += f"   Why:      {reason}\n"
        if job['url']:
            body += f"   Apply:    {job['url']}\n"
        body += "\n"

    body += "─" * 50 + "\n"
    body += "All results have been saved to your Notion database.\n"
    body += "\nGood luck! 🚀"

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
# MAIN — runs everything in sequence
# ─────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"🤖 Job Search Agent — {datetime.today().strftime('%d %b %Y')}")
    print(f"{'='*50}\n")

    # Step 1: Search
    jobs = search_jobs()

    if not jobs:
        print("No jobs found today.")
        return

    # Step 2 & 3: Score each job and save to Notion
    high_scoring_jobs = []
    print(f"\n📊 Scoring and saving {len(jobs)} jobs...\n")

    for job in jobs:
        print(f"  Scoring: {job['title']} @ {job['company']}")
        score, reason = score_job(job)
        save_to_notion(job, score, reason)

        if score >= ALERT_THRESHOLD:
            high_scoring_jobs.append((job, score, reason))

    # Step 4: Send alert if strong matches found
    print(f"\n📧 {len(high_scoring_jobs)} jobs scored {ALERT_THRESHOLD}+/10")
    send_alert_email(high_scoring_jobs)

    print(f"\n✅ Done! All results saved to Notion.\n")


if __name__ == "__main__":
    main()
