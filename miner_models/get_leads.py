import random
import json
from urllib.parse import urlparse
import asyncio
import aiohttp
import bittensor as bt
import os
import re

HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "YOUR_HUNTER_API_KEY")
CLEARBIT_API_KEY = os.getenv("CLEARBIT_API_KEY", "YOUR_CLEARBIT_API_KEY")
COMPANY_LIST_URL = "https://raw.githubusercontent.com/Pranavmr100/Sample-Leads/refs/heads/main/sampleleads.json"

industry_keywords = {
    "Tech & AI": ["saas", "software", "cloud", "subscription", "platform", "app", "online", "hosted", "crm", "erp", "ai", "machine learning", "artificial intelligence", "automation", "data", "analytics", "predictive", "nlp", "robotics", "algorithm", "cybersecurity", "security", "privacy", "protection", "encryption", "firewall", "antivirus", "network", "tech", "digital", "systems", "computing", "internet", "agent", "API"],
    "Finance & Fintech": ["fintech", "finance", "bank", "payment", "blockchain", "crypto", "lending", "investment", "money", "trading", "wallet", "transaction", "capital", "insurance", "wealth", "loan", "budget", "financial", "accounting", "stock", "exchange", "credit", "debit", "ledger", "token", "currency", "remittance", "crowdfunding", "mortgage", "billing", "payroll", "audit", "web3", "crypto", "blockchain", "decentralized"],
    "Health & Wellness": ["health", "medical", "biotech", "pharma", "hospital", "clinic", "care", "wellness", "patient", "diagnostics", "genomics", "therapy", "medicine", "drug", "research", "clinical", "biology", "edtech", "education", "learning", "training", "e-learning", "course", "lms", "tutor", "study", "telemedicine", "fitness", "nutrition", "surgery", "rehabilitation", "genetics", "skincare", "skin"],
    "Media & Education": ["media", "entertainment", "streaming", "content", "gaming", "advertising", "digital media", "video", "music", "broadcast", "film", "television", "radio", "publishing", "news", "marketing", "social", "production", "animation", "vr", "ar", "podcast", "event", "promotion", "creative", "design", "journalism", "cinema", "theater", "branding", "influencer", "engagement", "education", "edu"],
    "Energy & Industry": ["energy", "sustainability", "renewable", "clean tech", "green", "environment", "solar", "wind", "carbon", "eco-friendly", "manufacturing", "supply chain", "logistics", "production", "b2b", "industrial", "warehouse", "assembly", "shipping", "factory", "engineering", "infrastructure", "mining", "agriculture", "construction", "materials", "transport", "power", "recycling", "automation", "machinery", "distribution"]
}

VALID_INDUSTRIES = list(industry_keywords.keys())

def is_valid_email(email: str) -> bool:
    if not email or email.lower() == "no email":
        return False
    email_regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    return bool(email_regex.match(email))

def is_valid_website(website: str) -> bool:
    if not website:
        return False
    parsed_url = urlparse(website)
    return bool(parsed_url.scheme in ["http", "https"] and parsed_url.netloc)

async def fetch_industry_from_api(domain):
    if bt.config().mock:
        return "Tech & AI"
    url = f"https://company.clearbit.com/v1/domains/{domain}"
    headers = {"Authorization": f"Bearer {CLEARBIT_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
                sector = data.get("category", {}).get("sector", "").lower()
                if "software" in sector or "technology" in sector:
                    return "Tech & AI"
                elif "finance" in sector:
                    return "Finance & Fintech"
                elif "health" in sector or "education" in sector:
                    return "Health & Wellness"
                elif "media" in sector or "marketing" in sector:
                    return "Media & Education"
                elif "energy" in sector or "manufacturing" in sector:
                    return "Energy & Industry"
                return None
    except Exception:
        return None

async def assign_industry(name, website):
    if bt.config().mock:
        return "Tech & AI"
    domain = urlparse(website).netloc if website else ""
    name_lower = name.lower() if name else ""
    website_lower = website.lower() if website else ""
    if domain:
        api_industry = await fetch_industry_from_api(domain)
        if api_industry in VALID_INDUSTRIES:
            return api_industry
    industry_scores = {industry: 0 for industry in industry_keywords}
    for industry, keywords in industry_keywords.items():
        for keyword in keywords:
            if (name_lower and keyword in name_lower) or (website_lower and keyword in website_lower):
                industry_scores[industry] += 1
    max_score = max(industry_scores.values())
    if max_score > 0:
        top_industries = [ind for ind, score in industry_scores.items() if score == max_score]
        return top_industries[0]
    return "Tech & AI"

async def get_emails_hunter(domain):
    if bt.config().mock:
        return []
    url = f"https://api.hunter.io/v2/domain-search?domain={domain}&api_key={HUNTER_API_KEY}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
                emails = data.get("data", {}).get("emails", [])
                return [email["value"] for email in emails]
    except Exception:
        return []

async def get_leads(num_leads: int, industry: str = None, region: str = None) -> list:
    bt.logging.debug(f"Generating {num_leads} leads, industry={industry}, region={region}")
    leads = []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(COMPANY_LIST_URL, timeout=30) as response:
                response.raise_for_status()
                businesses = json.loads(await response.text())
                random.shuffle(businesses)
    except Exception as e:
        bt.logging.error(f"Failed to fetch businesses: {e}")
        businesses = []

    for business in businesses:
        if len(leads) >= num_leads:
            break
        name = business.get("Business", "")
        website = business.get("Website", "")
        if not is_valid_website(website):
            continue
        assigned_industry = await assign_industry(name, website)
        if industry and assigned_industry.lower() != industry.lower():
            continue
        business_region = business.get("Location", "")
        if region and business_region.lower() != region.lower():
            continue
        domain = urlparse(website).netloc if website else ""
        json_emails = business.get("Owner(s) Email", "").split("/") if business.get("Owner(s) Email") else []
        json_emails = [email for email in json_emails if is_valid_email(email)]
        if not json_emails:
            continue
        if domain and not bt.config().mock:
            hunter_emails = await get_emails_hunter(domain)
            all_emails = list(set(json_emails + hunter_emails))
        else:
            all_emails = json_emails
        if not all_emails:
            continue
        lead = {
            "Business": name,
            "Owner Full name": business.get("Owner Full name", ""),
            "First": business.get("First", ""),
            "Last": business.get("Last", ""),
            "Owner(s) Email": all_emails[0],
            "LinkedIn": business.get("LinkedIn", ""),
            "Website": website,
            "Industry": assigned_industry,
            "Region": business_region or "Unknown"
        }
        leads.append(lead)
    
    if len(leads) < num_leads:
        for i in range(len(leads), num_leads):
            leads.append({
                "Business": f"Fallback Business {i+1}",
                "Owner Full name": f"Owner {i+1}",
                "First": f"First {i+1}",
                "Last": f"Last {i+1}",
                "Owner(s) Email": f"fallback{i+1}@example.com",
                "LinkedIn": f"https://linkedin.com/in/fallback{i+1}",
                "Website": f"https://fallback{i+1}.com",
                "Industry": industry or "Tech & AI",
                "Region": region or "Global"
            })
    
    bt.logging.debug(f"Generated {len(leads)} leads")
    return leads