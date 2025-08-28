#LEGACY MODEL - Use Lead_sorcerer_main for Production, this should only be used for testing locally

import random
import json
from urllib.parse import urlparse
import asyncio
import aiohttp
import bittensor as bt
import os
import re
from Leadpoet.base.utils import safe_json_load
import csv
import time
import gzip
import hashlib
import signal
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from datetime import datetime, timedelta
import aiodns
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import functools
import dns.asyncresolver
import structlog
import redis.asyncio as redis
import itertools
from dotenv import load_dotenv
import logging
from collections import OrderedDict
import requests, textwrap

# Load .env file
load_dotenv()

# Only import Firecrawl if we're in a miner context (not validator)
# This prevents the validator from asking for Firecrawl API keys
try:
    FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
    FIRECRAWL_AVAILABLE = bool(FIRECRAWL_API_KEY and FIRECRAWL_API_KEY.strip())
except ImportError:
    FIRECRAWL_AVAILABLE = False

HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "YOUR_HUNTER_API_KEY")
CLEARBIT_API_KEY = os.getenv("CLEARBIT_API_KEY", "YOUR_CLEARBIT_API_KEY")
COMPANY_LIST_URL = "https://raw.githubusercontent.com/Pranavmr100/Sample-Leads/refs/heads/main/sampleleads.json"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Firecrawl headers
if FIRECRAWL_AVAILABLE:
    HEADERS_FC = {"Authorization": f"Bearer {FIRECRAWL_API_KEY}"}
else:
    HEADERS_FC = None

# Ensure logs directory exists
Path("logs").mkdir(exist_ok=True)

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Configure standard library logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/pipeline.log", mode="a", encoding="utf-8")
    ]
)

logger = structlog.get_logger()

# Cross-platform file locking
try:
    import fcntl
    def lock_file(file_obj, exclusive=True):
        """Lock file for exclusive or shared access (Unix)."""
        if exclusive:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)
        else:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_SH)
    
    def unlock_file(file_obj):
        """Unlock file (Unix)."""
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
        
except ImportError:
    # Windows fallback - no file locking, but log warning
    def lock_file(file_obj, exclusive=True):
        """No-op lock for Windows (not supported)."""
        logger.warning("file_locking_not_available_windows", note="Multiple processes may cause race conditions")
        pass
    
    def unlock_file(file_obj):
        """No-op unlock for Windows."""
        pass

# ------------------------------------------------------------------
#  Guarantee that the domains CSV exists (header: domain,last_scraped)
# ------------------------------------------------------------------
def ensure_domains_file(path: str):
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if not csv_path.exists():
        csv_path.write_text("domain,last_scraped\n", encoding="utf-8")
        logger.info("domains_csv_created", file=str(csv_path))

# ---------------- Global State ----------------
ARGS = type('Args', (), {
    'domains': os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "domains.csv"),
    'max_pages_per_domain': 50,
    'batch_size': 10,
    'flush_sec': 60,
    'redis_url': os.getenv('REDIS_URL', "redis://localhost:6379"),
    'web_port': 8080,
    'metrics_port': 9090,
    'firecrawl_key': FIRECRAWL_API_KEY,
    'hunter_key': HUNTER_API_KEY,
    'openrouter_key': OPENROUTER_API_KEY,
    'pdl_key': os.getenv('PDL_API_KEY'),
})()

# Make sure the CSV is present BEFORE anything tries to read/write it
ensure_domains_file(ARGS.domains)

# Global state for graceful shutdown
shutdown_requested = False
current_batch = []
outbox_file = None

def signal_handler(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    global shutdown_requested
    logger.info("shutdown_signal_received", signal=signum)
    shutdown_requested = True

# Register signal handler
signal.signal(signal.SIGTERM, signal_handler)

# ---------------- Prometheus Metrics ----------------
# Only define these if prometheus_client is available
try:
    from prometheus_client import Counter, Histogram, start_http_server, Gauge
    
    LEADS_LEGIT_TOTAL = Counter('leads_legit_total', 'Total legitimate leads processed')
    ENRICH_CALLS_TOTAL = Counter('enrich_calls_total', 'Total enrichment API calls')
    LEAD_LATENCY_SECONDS = Histogram('lead_latency_seconds', 'Time from extract to enriched')
    CRAWL_CREDITS_SPENT_TOTAL = Counter('crawl_credits_spent_total', 'Total crawl credits spent')
    LEGITIMACY_CHECKED_TOTAL = Counter('legitimacy_checked_total', 'Total emails checked for legitimacy')
    LEGITIMACY_PASS_TOTAL = Counter('legitimacy_pass_total', 'Total emails passing legitimacy check')
    BATCH_FLUSH_TOTAL = Counter('batch_flush_total', 'Total batch flushes', ['reason'])
    BATCH_SIZE_GAUGE = Gauge('batch_size', 'Configured batch size for enrichment')
    FLUSH_SEC_GAUGE = Gauge('flush_sec', 'Configured flush timeout in seconds')
    ENRICH_RECORDS_TOTAL = Counter('enrich_records_total', 'Total records enriched')
    ENRICH_CREDITS_TOTAL = Counter('enrich_credits_total', 'Total enrichment credits spent')
    FIRECRAWL_ENRICH_RECORDS_TOTAL = Counter('firecrawl_enrich_records_total', 'Total records enriched via Firecrawl')
    
    # Define a no-op function for when metrics aren't available
    def inc_counter(counter):
        counter.inc()
    
    def inc_counter_with_label(counter, label):
        counter.labels(label).inc()
    
    def set_gauge(gauge, value):
        gauge.set(value)
        
except ImportError:
    # Create no-op versions when prometheus_client is not available
    class NoOpMetric:
        def inc(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
        def set(self, *args, **kwargs): pass
    
    LEADS_LEGIT_TOTAL = NoOpMetric()
    ENRICH_CALLS_TOTAL = NoOpMetric()
    LEAD_LATENCY_SECONDS = NoOpMetric()
    CRAWL_CREDITS_SPENT_TOTAL = NoOpMetric()
    LEGITIMACY_CHECKED_TOTAL = NoOpMetric()
    LEGITIMACY_PASS_TOTAL = NoOpMetric()
    BATCH_FLUSH_TOTAL = NoOpMetric()
    BATCH_SIZE_GAUGE = NoOpMetric()
    FLUSH_SEC_GAUGE = NoOpMetric()
    ENRICH_RECORDS_TOTAL = NoOpMetric()
    ENRICH_CREDITS_TOTAL = NoOpMetric()
    FIRECRAWL_ENRICH_RECORDS_TOTAL = NoOpMetric()
    
    def inc_counter(counter): pass
    def inc_counter_with_label(counter, label): pass
    def set_gauge(gauge, value): pass

# ---------------- Legitimacy helpers ----------------
async def is_legit(email: str, mx_check=None) -> bool:
    """Check if email is legitimate using EmailValidator."""
    LEGITIMACY_CHECKED_TOTAL.inc()
    
    # Basic email format validation
    if not email or not isinstance(email, str):
        return False
    
    email_regex = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")
    if not email_regex.match(email.lower().strip()):
        return False
    
    # Check disposable domains
    disposable_domains_file = os.path.join(os.path.dirname(__file__), "shared/disposable_email_blocklist.conf")
    try:
        with open(disposable_domains_file, "r", encoding="utf-8") as f:
            disposable_domains = {line.strip().lower() for line in f if line.strip() and not line.startswith('#')}
    except FileNotFoundError:
        print(f"Warning: {disposable_domains_file} not found, using empty set")
        disposable_domains = set()
    
    domain = email.split("@")[-1]
    if domain in disposable_domains:
        return False
    
    # Use provided mx_check or basic domain check
    if mx_check is None:
        # Basic domain check - just verify it's not obviously invalid
        return len(domain) > 0 and '.' in domain
    
    try:
        has_mx = await mx_check(domain)
        if has_mx:
            LEGITIMACY_PASS_TOTAL.inc()
        return has_mx
    except Exception:
        return False

# ---------------- Crawl & extract ----------------
EXTRACT_SCHEMA = {
    "email": "",
    "first_name": "",
    "last_name": "",
    "title": "",
    "company": "",
    "url": "",
    "source_domain": "",
    "timestamp": "",
    "enrichment": {}
}

# URL patterns for contact extraction
INCLUDE_PATTERNS = ["/contact", "/about", "/team", "/legal", "/privacy", "/impressum"]
EXCLUDE_PATTERNS = ["/admin", "/login", "/api", "/assets", "/static"]

def should_include_url(url: str) -> bool:
    """Check if URL should be included for contact extraction."""
    for pattern in INCLUDE_PATTERNS:
        if re.search(pattern, url, re.I):
            return True
    return False

def should_exclude_url(url: str) -> bool:
    """Check if URL should be excluded."""
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, url, re.I):
            return True
    return False

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def extract_contacts(session: aiohttp.ClientSession, url: str, source_domain: str) -> List[Dict[str, Any]]:
    """Extract contacts from a single URL."""
    if not FIRECRAWL_AVAILABLE:
        return []
    
    try:
        async with session.post(
            "https://api.firecrawl.dev/v1/extract",
            headers=HEADERS_FC,
            json={"urls": [url]},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            response.raise_for_status()
            result = await response.json()
            
            contacts = []
            
            # Create basic contact record for contact pages
            if "/contact" in url:
                domain_name = source_domain.replace('https://', '').replace('http://', '')
                contact_data = {
                    "email": f"contact@{domain_name}",
                    "first_name": "Contact",
                    "last_name": "Team",
                    "title": "Contact",
                    "company": domain_name,
                    "url": url,
                    "source_domain": source_domain,
                    "timestamp": datetime.utcnow().isoformat(),
                    "enrichment": {}
                }
                contacts.append(contact_data)
            
            # If API returns contact data, extract it
            if "data" in result and "contacts" in result["data"]:
                for contact in result["data"]["contacts"]:
                    full_name = contact.get("name", "")
                    first_name = ""
                    last_name = ""
                    if full_name:
                        name_parts = full_name.strip().split()
                        if len(name_parts) >= 2:
                            first_name = name_parts[0]
                            last_name = " ".join(name_parts[1:])
                        else:
                            first_name = full_name
                    
                    contact_data = {
                        "email": contact.get("email", ""),
                        "first_name": first_name,
                        "last_name": last_name,
                        "title": contact.get("title", ""),
                        "company": contact.get("company", ""),
                        "url": url,
                        "source_domain": source_domain,
                        "timestamp": datetime.utcnow().isoformat(),
                        "enrichment": {}
                    }
                    contacts.append(contact_data)
            
            return contacts
            
    except Exception as e:
        print(f"Error extracting contacts from {url}: {e}")
        return []

async def crawl_domain(domain: str) -> List[Dict[str, Any]]:
    """Crawl a domain and extract contact information."""
    if not FIRECRAWL_AVAILABLE:
        return []
    
    start_time = time.time()
    
    try:
        # Check if domain was recently scraped
        if is_recently_scraped(domain):
            return []
        
        # Crawl domain
        crawl_data = await firecrawl_crawl(domain)
        if not crawl_data:
            # Mark domain as invalid since crawl failed
            update_last_scraped(domain, "invalid")
            return []
        
        # Extract eligible URLs from crawl data
        eligible_urls = []
        for page in crawl_data:
            url = page.get("metadata", {}).get("sourceURL")
            if url and should_include_url(url):
                eligible_urls.append(url)
        
        if not eligible_urls:
            # Mark domain as valid but no leads found
            update_last_scraped(domain, "valid")
            return []
        
        # Extract contacts from eligible URLs
        contacts = await extract_contacts_from_urls(eligible_urls, domain)
        
        # Filter for legitimate emails
        legitimate_contacts = []
        for contact in contacts:
            email = contact.get("email", "")
            if email and await is_legit(email):
                legitimate_contacts.append(contact)
        
        # Update domain status based on results
        if legitimate_contacts:
            update_last_scraped(domain, "valid")
        else:
            update_last_scraped(domain, "valid")  # Still valid, just no leads
        
        crawl_time = time.time() - start_time
        
        return legitimate_contacts
        
    except Exception as e:
        # Mark domain as invalid due to error
        update_last_scraped(domain, "invalid")
        return []

def load_domains(domains_file: str) -> List[str]:
    """Load domains from CSV file, filtering out deactivated/invalid domains."""
    ensure_domains_file(domains_file)
    try:
        domains = []
        with open(domains_file, 'r', encoding='utf-8') as f:
            # Read all lines and extract domains (remove trailing comma)
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    parts = line.split(',')
                    if len(parts) >= 1:
                        domain = parts[0].rstrip(',')
                        
                        # Check if domain is marked as deactivated/invalid
                        if len(parts) >= 3 and "deactivated/invalid" in parts[2]:
                            logger.debug("domain_skipped_deactivated", domain=domain)
                            continue
                        
                        domains.append(domain)
        
        # Only log the count, not the verbose JSON
        print(f"📋 Loaded {len(domains)} valid domains from {domains_file}")
        return domains
    except Exception as e:
        print(f"❌ Error loading domains: {e}")
        return []

def get_random_domains(domains_file: str, sample_size: int = 1) -> List[str]:
    """Get a random sample of domains from the CSV file."""
    all_domains = load_domains(domains_file)
    if not all_domains:
        return []
    
    # Sample without replacement
    sample_size = min(sample_size, len(all_domains))
    return random.sample(all_domains, sample_size)

def update_last_scraped(domain: str, status: str = "valid"):
    """Update the last_scraped timestamp and status for a domain with file locking."""
    try:
        domains_file = ARGS.domains
        ensure_domains_file(domains_file)
        
        # Open file for reading and writing
        with open(domains_file, 'r+', encoding='utf-8') as f:
            # Acquire exclusive lock
            lock_file(f, True)
            
            try:
                # Read all domains
                f.seek(0)
                lines = f.readlines()
                
                # Update the specific domain
                updated = False
                for i, line in enumerate(lines):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Extract domain (remove trailing comma)
                        current_domain = line.rstrip(',')
                        
                        if current_domain == domain:
                            # Update the line with timestamp and status
                            timestamp = datetime.utcnow().isoformat() + 'Z'
                            if status == "valid":
                                new_line = f"{domain},{timestamp},domain valid\n"
                            else:
                                new_line = f"{domain},{timestamp},domain deactivated/invalid\n"
                            lines[i] = new_line
                            updated = True
                            break
                
                if not updated:
                    logger.warning("domain_not_found_for_update", domain=domain)
                    return
                
                # Write back to file
                f.seek(0)
                f.truncate()  # Clear file content
                f.writelines(lines)
                
                logger.debug("domain_timestamp_updated", domain=domain, status=status)
                
            finally:
                # Release lock
                unlock_file(f)
            
    except Exception as e:
        logger.error("update_last_scraped_failed", domain=domain, error=str(e))
        raise

def is_recently_scraped(domain: str) -> bool:
    """Check if domain was recently scraped (within 24 hours)."""
    try:
        domains_file = ARGS.domains
        ensure_domains_file(domains_file)
        
        with open(domains_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',')
                    if len(parts) >= 2:
                        current_domain = parts[0]
                        if current_domain == domain:
                            # Check if there's a timestamp
                            if len(parts) >= 2 and parts[1]:
                                try:
                                    # Parse the timestamp
                                    timestamp_str = parts[1]
                                    if timestamp_str.endswith('Z'):
                                        timestamp_str = timestamp_str[:-1]  # Remove Z
                                    last_scraped = datetime.fromisoformat(timestamp_str)
                                    now = datetime.utcnow()
                                    
                                    # Check if it's been less than 24 hours
                                    time_diff = now - last_scraped
                                    if time_diff.total_seconds() < 24 * 3600:  # 24 hours in seconds
                                        logger.debug("domain_recently_scraped", domain=domain, hours_ago=time_diff.total_seconds() / 3600)
                                        return True
                                except Exception as e:
                                    logger.warning("timestamp_parse_failed", domain=domain, timestamp=parts[1], error=str(e))
                                    return False
                            break
        
        return False
        
    except Exception as e:
        logger.error("recent_scrape_check_failed", domain=domain, error=str(e))
        return False

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def firecrawl_crawl(domain: str) -> Optional[List[Dict[str, Any]]]:
    """Crawl domain using Firecrawl API with retry logic."""
    if not FIRECRAWL_AVAILABLE:
        return []
    
    try:
        async with aiohttp.ClientSession() as session:
            # Start crawl job
            async with session.post(
                "https://api.firecrawl.dev/v1/crawl",
                headers=HEADERS_FC,
                json={
                    "url": domain if domain.startswith(('http://', 'https://')) else f"https://{domain}",
                    "maxDepth": 6,
                    "limit": ARGS.max_pages_per_domain,
                    "allowBackwardLinks": True,
                    "includePaths": INCLUDE_PATTERNS,
                    "excludePaths": EXCLUDE_PATTERNS
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error("firecrawl_error", 
                               domain=domain,
                               status=response.status,
                               error=error_text,
                               headers=dict(response.headers))
                    response.raise_for_status()
                    
                job = await response.json()
                job_id = job["id"]
                logger.info("crawl_job_started", domain=domain, job_id=job_id)
            
            # Poll for completion with early-stop safeguard and 429 back-off
            crawl_start_time = time.time()
            retry_count = 0
            max_retries = 3
        
            max_crawl_time = 300
            while True:
                await asyncio.sleep(5)  # Poll interval
                
                # Early-stop safeguard: check if elapsed time > max_crawl_time
                elapsed_time = time.time() - crawl_start_time
                if elapsed_time > max_crawl_time:
                    logger.warning("crawl_timeout_exceeded", domain=domain, job_id=job_id, elapsed_time=elapsed_time)
                    break
                
                try:
                    async with session.get(
                        f"https://api.firecrawl.dev/v1/crawl/{job_id}",
                        headers=HEADERS_FC,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        if response.status != 200:
                            error_text = await response.text()
                            logger.error("firecrawl_status_error", 
                                       domain=domain,
                                       job_id=job_id,
                                       status=response.status,
                                       error=error_text,
                                       headers=dict(response.headers))
                            response.raise_for_status()
                            
                        status = await response.json()
                        
                        pages = status.get("data", [])
                        if pages:
                            urls = [p["metadata"]["sourceURL"] for p in pages if "metadata" in p and "sourceURL" in p["metadata"]]
                            logger.info("crawl_progress", domain=domain, job_id=job_id, pages_found=len(urls))
                            
                            # Update Prometheus metric for crawl credits spent
                            CRAWL_CREDITS_SPENT_TOTAL.inc(len(urls))
                        
                        if status.get("status") == "completed":
                            logger.info("crawl_completed", domain=domain, job_id=job_id, pages_found=len(pages))
                            return pages
                        
                        logger.info("crawl_status", domain=domain, job_id=job_id, status=status.get('status'))
                        
                except aiohttp.ClientResponseError as e:
                    if e.status == 429:
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.error("crawl_rate_limit_exceeded", domain=domain, job_id=job_id)
                            return None
                        
                        logger.warning("crawl_rate_limited", domain=domain, job_id=job_id, retry_count=retry_count)
                        await asyncio.sleep(30)
                        continue
                    else:
                        logger.error("firecrawl_status_exception", 
                                   domain=domain,
                                   job_id=job_id,
                                   error=str(e),
                                   error_type=type(e).__name__)
                        raise
            
            return None
                    
    except Exception as e:
        logger.error("firecrawl_exception", 
                     domain=domain,
                     error=str(e),
                     error_type=type(e).__name__)
        raise

async def firecrawl_extract_contacts(session, url):
    """Call Firecrawl /v1/extract for a single URL and return contacts list."""
    if not FIRECRAWL_AVAILABLE:
        return []
    
    try:
        logger.info("extract_api_call_started", url=url)
        
        # Step 1: Start the extract job
        async with session.post(
            "https://api.firecrawl.dev/v1/extract",
            headers=HEADERS_FC,
            json={"urls": [url]},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            logger.info("extract_api_response_received", url=url, status=response.status)
            response.raise_for_status()
            result = await response.json()
            logger.info("extract_api_response_parsed", url=url, result_keys=list(result.keys()) if isinstance(result, dict) else "Not a dict")
            logger.debug("extract_api_full_response", url=url, response=result)
            
            if not result.get("success") or "id" not in result:
                logger.error("extract_job_failed", url=url, result=result)
                return []
            
            job_id = result["id"]
            logger.info("extract_job_started", url=url, job_id=job_id)
            
        # Step 2: Poll the extract job until completion
        max_wait_time = 300  # 5 minutes
        poll_interval = 5
        elapsed_time = 0
        
        while elapsed_time < max_wait_time:
            await asyncio.sleep(poll_interval)
            elapsed_time += poll_interval
            
            logger.info("extract_job_polling", url=url, job_id=job_id, elapsed=elapsed_time)
            
            async with session.get(
                f"https://api.firecrawl.dev/v1/extract/{job_id}",
                headers=HEADERS_FC,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as poll_response:
                poll_response.raise_for_status()
                poll_result = await poll_response.json()
                logger.debug("extract_job_poll_response", url=url, job_id=job_id, result=poll_result)
                
                status = poll_result.get("status", "unknown")
                logger.info("extract_job_status", url=url, job_id=job_id, status=status)
                
                if status == "completed":
                    # Step 3: Parse the completed results
                    logger.info("extract_job_completed", url=url, job_id=job_id)
                    return await parse_extract_results(session, url, poll_result)
                elif status == "failed":
                    logger.error("extract_job_failed", url=url, job_id=job_id, result=poll_result)
                    break
                elif status in ["pending", "processing"]:
                    continue
                else:
                    logger.warning("extract_job_unknown_status", url=url, job_id=job_id, status=status)
                    break
        
        logger.error("extract_job_timeout", url=url, job_id=job_id, max_wait_time=max_wait_time)
        return await fallback_heuristic_contact(url)
        
    except Exception as e:
        logger.error("extract_api_error", url=url, error=str(e), error_type=type(e).__name__)
        return await fallback_heuristic_contact(url)

async def parse_extract_results(session, url, poll_result):
    """Parse contacts from the completed extract job results."""
    contacts = []
    
    try:
        # Check if we have data with contacts
        if "data" in poll_result and "contacts" in poll_result["data"]:
            logger.info("contacts_found_in_api_response", url=url, contact_count=len(poll_result['data']['contacts']))
            for contact in poll_result["data"]["contacts"]:
                logger.debug("processing_contact", url=url, contact=contact)
                full_name = contact.get("name", "")
                first_name = ""
                last_name = ""
                if full_name:
                    name_parts = full_name.strip().split()
                    if len(name_parts) >= 2:
                        first_name = name_parts[0]
                        last_name = " ".join(name_parts[1:])
                    else:
                        first_name = full_name
                email = contact.get("email", "").strip()
                if email and "@" not in email:
                    logger.warning("invalid_email_format", url=url, email=email)
                    email = ""
                contact_data = {
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "title": contact.get("title", "").strip(),
                    "company": contact.get("company", "").strip(),
                    "url": url,
                    "source_domain": url.split("/")[2] if url.startswith("http") else url,
                    "timestamp": datetime.utcnow().isoformat() + 'Z',
                    "enrichment": {}
                }
                if email or first_name or last_name:
                    contacts.append(contact_data)
                    logger.info("contact_added", url=url, contact_data=contact_data)
                else:
                    logger.warning("contact_skipped_no_data", url=url, contact_data=contact_data)
        else:
            logger.info("no_contacts_in_api_response", url=url, data_keys=list(poll_result.get('data', {}).keys()) if isinstance(poll_result.get('data'), dict) else "No data field")
        
        # Check if we should use heuristic fallback
        if not contacts and "/contact" in url:
            logger.info("using_heuristic_fallback", url=url)
            heuristic_contact = await create_heuristic_contact(url)
            contacts.append(heuristic_contact)
            logger.info("heuristic_contact_created", url=url, contact_data=heuristic_contact)
        elif not contacts:
            logger.info("no_contacts_and_no_heuristic", url=url)
        
        logger.info("extract_completed", url=url, contacts_found=len(contacts))
        return contacts
        
    except Exception as e:
        logger.error("parse_extract_results_error", url=url, error=str(e))
        return await fallback_heuristic_contact(url)

async def create_heuristic_contact(url):
    """Create a heuristic contact for /contact pages."""
    domain_name = url.replace('https://', '').replace('http://', '').split('/')[0]
    return {
        "email": f"contact@{domain_name}",
        "first_name": "",
        "last_name": "",
        "title": "Contact Page",
        "company": domain_name,
        "url": url,
        "source_domain": domain_name,
        "timestamp": datetime.utcnow().isoformat() + 'Z',
        "enrichment": {}
    }

async def fallback_heuristic_contact(url):
    """Fallback to heuristic contact for /contact pages on any error."""
    contacts = []
    if "/contact" in url:
        logger.info("using_heuristic_fallback_on_error", url=url)
        heuristic_contact = await create_heuristic_contact(url)
        contacts.append(heuristic_contact)
        logger.info("heuristic_contact_created_on_error", url=url, contact_data=heuristic_contact)
    return contacts

async def extract_contacts_from_urls(urls, source_domain):
    """Extract contacts from all eligible URLs using Firecrawl extract API."""
    contacts = []
    async with aiohttp.ClientSession() as session:
        for url in urls:
            url_contacts = await firecrawl_extract_contacts(session, url)
            contacts.extend(url_contacts)
            await asyncio.sleep(1)  # avoid rate limiting
    return contacts

# ---------------- Industry Classification ----------------
# ─────────────────────────── NEW CONFIG ────────────────────────────
LEADS_PER_DOMAIN = 1          # send every lead immediately
# very small seed list – extend freely
INDUSTRY_KEYWORDS = {
    "saas":          "Tech & AI",
    "software":      "Tech & AI",
    "ai":            "Tech & AI",
    "machine learning": "Tech & AI",
    "blockchain":    "Finance & Fintech",
    "crypto":        "Finance & Fintech",
    "bank":          "Finance & Fintech",
    "clinic":        "Health & Wellness",
    "hospital":      "Health & Wellness",
    "fitness":       "Health & Wellness",
    "solar":         "Energy & Industry",
    "oil":           "Energy & Industry",
    "manufacturing": "Energy & Industry",
    # …add more here…
}

def _infer_industry(text: str) -> str:
    """
    Very fast rule-based classifier using the Firecrawl title/description.
    Returns first match or 'Unknown'.
    """
    txt = text.lower()
    for kw, ind in INDUSTRY_KEYWORDS.items():
        if kw in txt:
            return ind
    return "Unknown"

# ────────────────────────── TAXONOMY ────────────────────────────
# Ordered so the first hit "wins" when multiple keywords appear
SUB_INDUSTRY_KEYWORDS: "OrderedDict[str, tuple[str,list[str]]]" = OrderedDict([
    # Tech & AI  (20)
    ("SaaS",             ("Tech & AI", ["saas", "subscription software"])),
    ("Cloud Computing",  ("Tech & AI", ["cloud", "aws", "azure", "gcp"])),
    ("Cyber Security",   ("Tech & AI", ["cybersecurity", "firewall", "antivirus", "infosec"])),
    ("E-Commerce Tech",  ("Tech & AI", ["e-commerce platform", "shopify", "woocommerce"])),
    ("Blockchain",       ("Tech & AI", ["blockchain", "web3", "ethereum", "bitcoin"])),
    ("FinTech Stack",    ("Tech & AI", ["payment gateway", "stripe api", "banking-as-a-service"])),
    ("EdTech",           ("Tech & AI", ["edtech", "learning management system", "lms"])),
    ("MarTech",          ("Tech & AI", ["marketing automation", "crm", "hubspot"])),
    ("HR Tech",          ("Tech & AI", ["hr software", "talent platform", "ats"])),
    ("PropTech",         ("Tech & AI", ["proptech", "real-estate tech"])),
    ("Health IT",        ("Tech & AI", ["ehr", "emr", "telehealth", "medtech software"])),
    ("AI LLM",           ("Tech & AI", ["ai", "machine learning", "deep learning", "llm"])),
    ("Robotics",         ("Tech & AI", ["robotics", "autonomous", "drone"])),
    ("IoT",              ("Tech & AI", ["iot device", "internet of things"])),
    ("AR / VR",          ("Tech & AI", ["augmented reality", "virtual reality", "metaverse"])),
    ("Analytics",        ("Tech & AI", ["data analytics", "business intelligence", "bi tool"])),
    ("DevOps",           ("Tech & AI", ["ci/cd", "kubernetes", "devops"])),
    ("Gaming",           ("Tech & AI", ["game studio", "game development", "gaming platform"])),
    ("AdTech",           ("Tech & AI", ["ad network", "programmatic ads"])),
    ("General Software", ("Tech & AI", ["software development", "custom software"])),
    # Finance & Fintech (20)
    ("Neo Bank",         ("Finance & Fintech", ["neobank", "digital bank"])),
    ("Payment Gateway",  ("Finance & Fintech", ["payment gateway", "merchant services"])),
    ("Lending Platform", ("Finance & Fintech", ["p2p lending", "loan platform", "mortgage tech"])),
    ("Wealth Tech",      ("Finance & Fintech", ["wealth management", "robo-advisor"])),
    ("InsurTech",        ("Finance & Fintech", ["insurtech", "insurance platform"])),
    ("Accounting SaaS",  ("Finance & Fintech", ["accounting software", "bookkeeping platform"])),
    ("Crypto Exchange",  ("Finance & Fintech", ["crypto exchange", "defi", "dex"])),
    ("RegTech",          ("Finance & Fintech", ["regtech", "kyc", "aml"])),
    ("Payroll Tech",     ("Finance & Fintech", ["payroll software", "salary management"])),
    ("Point-of-Sale",    ("Finance & Fintech", ["pos system", "point of sale"])),
    ("Trading Platform", ("Finance & Fintech", ["trading platform", "brokerage"])),
    ("Tax Software",     ("Finance & Fintech", ["tax software", "tax filing"])),
    ("Expense Mgmt",     ("Finance & Fintech", ["expense management", "spend control"])),
    ("Invoice Factoring",("Finance & Fintech", ["invoice factoring", "receivables finance"])),
    ("Crowdfunding",     ("Finance & Fintech", ["crowdfunding", "kickstarter-like"])),
    ("Billing API",      ("Finance & Fintech", ["recurring billing", "stripe"])),
    ("Financial Data API",("Finance & Fintech",["open banking api", "plaid"])),
    ("Treasury Mgmt",    ("Finance & Fintech", ["treasury management", "cash management"])),
    ("ATM / Kiosk",      ("Finance & Fintech", ["atm network", "bank kiosk"])),
    ("Angel Network",    ("Finance & Fintech", ["angel network", "venture funding"])),
    # Health & Wellness (20)
    ("Hospital",         ("Health & Wellness", ["hospital", "clinic", "medical center"])),
    ("Telemedicine",     ("Health & Wellness", ["telemedicine", "telehealth"])),
    ("Mental Health App",("Health & Wellness", ["mental health app", "therapy app"])),
    ("Fitness Studio",   ("Health & Wellness", ["gym", "fitness center", "crossfit"])),
    ("Yoga Studio",      ("Health & Wellness", ["yoga studio", "pilates studio"])),
    ("Wellness Product", ("Health & Wellness", ["supplements", "nutraceutical"])),
    ("Pharma Company",   ("Health & Wellness", ["pharmaceutical", "drug discovery"])),
    ("Biotech",          ("Health & Wellness", ["biotech", "gene therapy"])),
    ("Medical Device",   ("Health & Wellness", ["medical device", "diagnostic device"])),
    ("Dental Clinic",    ("Health & Wellness", ["dental clinic", "dentist"])),
    ("EHR Vendor",       ("Health & Wellness", ["ehr", "emr platform"])),
    ("Health Insurance", ("Health & Wellness", ["health insurance", "payer"])),
    ("Nutrition Coaching",("Health & Wellness",["nutrition coach", "dietician"])),
    ("Spa / Wellness",   ("Health & Wellness", ["spa", "wellness retreat"])),
    ("Life Sciences",    ("Health & Wellness", ["life sciences", "clinical trial"])),
    ("Child Care",       ("Health & Wellness", ["childcare center", "daycare"])),
    ("Senior Care",      ("Health & Wellness", ["senior care", "assisted living"])),
    ("Sports Medicine",  ("Health & Wellness", ["sports medicine", "physiotherapy"])),
    ("Pet Health",       ("Health & Wellness", ["veterinary", "pet clinic"])),
    ("Healthcare Consulting",("Health & Wellness",["healthcare consulting"])),
    # Media & Education (20)
    ("University",       ("Media & Education", ["university", "college", "campus"])),
    ("K-12 School",      ("Media & Education", ["k-12", "elementary school", "high school"])),
    ("Online Course",    ("Media & Education", ["online course", "mooc", "udemy"])),
    ("Publishing House", ("Media & Education", ["publisher", "publishing house"])),
    ("News Outlet",      ("Media & Education", ["news", "newspaper", "magazine"])),
    ("Streaming Media",  ("Media & Education", ["streaming", "ott platform", "vod"])),
    ("Podcast Network",  ("Media & Education", ["podcast network", "podcasting"])),
    ("Marketing Agency", ("Media & Education", ["marketing agency", "advertising agency"])),
    ("Social Network",   ("Media & Education", ["social network", "social media platform"])),
    ("Gaming Studio",    ("Media & Education", ["game studio", "game publisher"])),
    ("Bookstore",        ("Media & Education", ["bookstore", "library"])),
    ("Language Learning",("Media & Education", ["language learning", "duolingo"])),
    ("Ed-Publishing",    ("Media & Education", ["educational publishing"])),
    ("E-Learning SaaS",  ("Media & Education", ["learning management system", "lms"])),
    ("PR Agency",        ("Media & Education", ["public relations", "pr agency"])),
    ("Photo Stock",      ("Media & Education", ["stock photos", "photo marketplace"])),
    ("Video Production", ("Media & Education", ["video production", "film studio"])),
    ("Music Label",      ("Media & Education", ["music label", "record label"])),
    ("Ticketing Platform",("Media & Education",["ticketing platform", "event tickets"])),
    ("e-Sports Org",     ("Media & Education", ["esports", "competitive gaming"])),
    # Energy & Industry (20)
    ("Solar Installer",  ("Energy & Industry", ["solar installer", "pv system"])),
    ("Wind Energy",      ("Energy & Industry", ["wind farm", "wind turbine"])),
    ("Oil & Gas",        ("Energy & Industry", ["oil field", "drilling", "refinery"])),
    ("Battery Tech",     ("Energy & Industry", ["battery technology", "lithium-ion"])),
    ("EV Charging",      ("Energy & Industry", ["ev charging", "charging station"])),
    ("Nuclear Energy",   ("Energy & Industry", ["nuclear power", "reactor"])),
    ("Water Utility",    ("Energy & Industry", ["water utility", "water treatment"])),
    ("Waste Management", ("Energy & Industry", ["waste management", "recycling"])),
    ("Manufacturing",    ("Energy & Industry", ["manufacturing", "factory"])),
    ("Logistics",        ("Energy & Industry", ["logistics", "supply chain"])),
    ("Aerospace",        ("Energy & Industry", ["aerospace", "space systems"])),
    ("Automotive",       ("Energy & Industry", ["automotive", "car manufacturer"])),
    ("Agritech",         ("Energy & Industry", ["agritech", "precision agriculture"])),
    ("Chemical Plant",   ("Energy & Industry", ["chemical plant", "chemicals"])),
    ("Mining",           ("Energy & Industry", ["mining", "mineral extraction"])),
    ("Construction",     ("Energy & Industry", ["construction", "civil engineering"])),
    ("HVAC Services",    ("Energy & Industry", ["hvac", "heating cooling"])),
    ("Packaging",        ("Energy & Industry", ["packaging", "labeling"])),
    ("Textiles",         ("Energy & Industry", ["textile mill", "fabric manufacturer"])),
    ("3D Printing",      ("Energy & Industry", ["3d printing", "additive manufacturing"])),
])

# ── OpenRouter model selection ────────────────────────────────
PRIMARY_MODEL  = "mistralai/mistral-small-3.2-24b-instruct:free"
FALLBACK_MODEL = "mistralai/mistral-7b-instruct"
# keep legacy name so the rest of the file still works
MODEL_NAME     = PRIMARY_MODEL

# ───────────────────── New taxonomy utils ─────────────────────────────
ALLOWED_INDUSTRIES = [
    "Marketing", "Technology", "Finance", "Healthcare", "Manufacturing",
    "Retail", "Education", "Real Estate", "Energy & Utilities",
    "Transportation & Logistics", "Media & Entertainment",
]

_LEGACY_TO_NEW = {
    "Tech & AI": "Technology",
    "Finance & Fintech": "Finance",
    "Health & Wellness": "Healthcare",
    "Media & Education": "Media & Entertainment",
    "Energy & Industry": "Energy & Utilities",
}

def _normalize_industry(ind: str) -> str:
    """Map any label → one of the 11 canonical buckets."""
    if not ind:
        return "Unknown"
    low = ind.lower().strip()

    for canon in ALLOWED_INDUSTRIES:
        if canon.lower() == low:
            return canon

    for old, new in _LEGACY_TO_NEW.items():
        if old.lower() == low:
            return new

    # heuristic fallback
    if any(k in low for k in ("tech", "software", "saas", "ai")):                           return "Technology"
    if any(k in low for k in ("finance", "fintech", "bank", "crypto", "payment")):         return "Finance"
    if any(k in low for k in ("health", "med", "clinic", "hospital", "pharma")):           return "Healthcare"
    if any(k in low for k in ("manufactur", "factory", "industrial")):                     return "Manufacturing"
    if any(k in low for k in ("retail", "e-commerce", "store", "shop")):                   return "Retail"
    if any(k in low for k in ("education", "edtech", "school", "learning")):               return "Education"
    if any(k in low for k in ("real estate", "proptech", "property")):                     return "Real Estate"
    if any(k in low for k in ("energy", "utility", "solar", "oil", "gas", "power")):       return "Energy & Utilities"
    if any(k in low for k in ("transport", "logistics", "shipping", "delivery")):          return "Transportation & Logistics"
    if any(k in low for k in ("media", "entertainment", "music", "video", "film", "game")): return "Media & Entertainment"
    if any(k in low for k in ("marketing", "advertising", "seo", "campaign")):             return "Marketing"
    return "Technology"  # safe default

# ----------------- updated keyword heuristic -------------------------
INDUSTRY_KEYWORDS = {
    "marketing":        "Marketing",
    "advertising":      "Marketing",
    "seo":              "Marketing",
    "saas":             "Technology",
    "software":         "Technology",
    "ai":               "Technology",
    "machine learning": "Technology",
    "blockchain":       "Finance",
    "crypto":           "Finance",
    "bank":             "Finance",
    "clinic":           "Healthcare",
    "hospital":         "Healthcare",
    "pharma":           "Healthcare",
    "fitness":          "Healthcare",
    "solar":            "Energy & Utilities",
    "oil":              "Energy & Utilities",
    "gas":              "Energy & Utilities",
    "manufacturing":    "Manufacturing",
    "retail":           "Retail",
    "e-commerce":       "Retail",
}

def _classify_industry(meta_txt: str) -> tuple[str, str]:
    print("\n🛈  LLM-CLASSIFY  INPUT ↓")
    print(meta_txt[:800])
    result = _llm_classify(meta_txt)
    if result:
        industry, sub, model_used = result
        industry = _normalize_industry(industry)
        print("🛈  LLM-CLASSIFY  OUTPUT ↓")
        print({"industry": industry, "sub_industry": sub})
        print(f"✅ LLM-CLASSIFY succeeded (model: {model_used})")
        return industry, sub

    print("⚠️  LLM-CLASSIFY failed – falling back to keyword heuristic")
    fallback_ind = _normalize_industry(_infer_industry(meta_txt))
    return fallback_ind, "General"

# ──────────────────────── CORE CRAWLER ──────────────────────────────
async def _crawl_domain(session, domain: str) -> list[dict]:
    """
    Crawl a domain, extract contacts & meta, return at most LEADS_PER_DOMAIN leads.
    """
    if not FIRECRAWL_AVAILABLE:
        return []
    
    api_url = "https://api.firecrawl.dev/v1/crawl"
    payload = {"url": f"https://{domain}", "depth": 0, "parseContacts": True}
    async with session.post(api_url, json=payload, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Firecrawl {response.status}")
        data = await response.json()

    contacts = data.get("contacts", []) or []
    meta_text = " ".join(filter(None, [
        data.get("title", ""),
        data.get("description", ""),
        " ".join(data.get("keywords", []))
    ]))

    industry, sub_ind = _classify_industry(meta_text)

    leads = []
    for contact in contacts:
        leads.append({
            "business": domain,
            "owner_full_name": contact.get("name", ""),
            "first": contact.get("first", ""),
            "last": contact.get("last", ""),
            "owner_email": contact["email"],
            "linkedin": contact.get("linkedin", ""),
            "website": contact.get("website") or f"https://{domain}",
            "industry": industry,
            "sub_industry": sub_ind,
            "region": "Global",
        })
        if len(leads) >= LEADS_PER_DOMAIN:
            break
    return leads

def _llm_classify(text: str) -> tuple[str, str, str] | None:
    """
    Classify a company snippet with OpenRouter.
    1️⃣ Try the free 24-B Mistral model.
    2️⃣ On error → fall back to Mistral-7B.
    Returns (industry, sub_industry, model_used) or None.
    """
    if not OPENROUTER_API_KEY:
        return None

    prompt_system = (
        "You are an industry classifier. "
        "Given short snippets (domain, page title / description, contact URL) "
        "return JSON ONLY: {\"industry\":\"<one of: "
        + " | ".join(ALLOWED_INDUSTRIES)
        + ">\", \"sub_industry\":\"<your best guess sub-industry>\"}"
    )
    prompt_user = textwrap.shorten(text, width=400, placeholder=" …")

    for model_name in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": prompt_system},
                        {"role": "user",   "content": prompt_user},
                    ],
                    "temperature": 0.2,
                },
                timeout=20,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.strip("`").lstrip("json").strip()
            j = json.loads(content)
            ind = j.get("industry")
            sub = j.get("sub_industry") or j.get("subIndustry")
            if ind and sub:
                return ind, sub, model_name
        except Exception as e:
            logger.warning("llm_classify_failed_attempt",
                           model=model_name,
                           error=str(e)[:200])
            # try next model
            continue

    return None

# ------------------------------------------------------------------
#  Public helper so the miner can ask "give me N leads please"
# ------------------------------------------------------------------
async def get_firecrawl_leads(num_leads: int,
                              industry: Optional[str] = None,
                              region:   Optional[str] = None) -> List[Dict]:
    """
    Get leads using Firecrawl API with domain filtering and deduplication.
    """
    if not FIRECRAWL_AVAILABLE:
        print("⚠️  Firecrawl not available - no API key")
        return []
    
    leads = []
    attempts = 0
    max_attempts = 20  # Increased to handle more domain attempts

    while len(leads) < num_leads and attempts < max_attempts:
        attempts += 1
        
        # Get a random sample of domains (try 1 domain at a time for efficiency)
        random_domains = get_random_domains(ARGS.domains, sample_size=1)
        
        if not random_domains:
            print("⚠️  No domains available")
            break
        
        domain = random_domains[0]
        print(f"🔄 Attempt {attempts}: Trying domain {domain}")
        
        try:
            # Try to crawl this domain
            contacts = await crawl_domain(domain)
            
            for c in contacts:
                email = c.get("email", "")
                
                # Skip if email already exists in pool
                if email and check_existing_contact(email):
                    print(f"⏭️  Skipping duplicate contact: {email}")
                    continue
                
                # ---- LLM classification ------------------------------------
                parts = [
                    domain,
                    c.get("company", ""),
                    c.get("title", ""),
                    c.get("url") or f"https://{domain}"
                ]
                # drop duplicate root-domain if company == domain
                seen = set()
                meta_text = " ".join(
                    p for p in parts if p and (p not in seen and not seen.add(p))
                )
                industry, sub_ind = _classify_industry(meta_text)

                # ---- map the Firecrawl contact object to legacy keys ----
                full_name = f"{c.get('first_name','')} {c.get('last_name','')}".strip()
                website   = c.get("url") or f"https://{domain}"
                mapped = {
                    "Business":        c.get("company") or domain,
                    "Owner Full name": full_name,
                    "First":           c.get("first_name", ""),
                    "Last":            c.get("last_name", ""),
                    "Owner(s) Email":  email,
                    "LinkedIn":        c.get("enrichment", {}).get("pdl", {}).get("linkedin", ""),
                    "Website":         website,
                    "Industry":        industry,
                    "sub_industry":    sub_ind,      # lower-case consistent
                    "role":            c.get("title", ""),
                    "Region":          region or "Global",
                }
                # basic filtering so we don't return empty rows
                if mapped["Owner(s) Email"]:
                    leads.append(mapped)

                if len(leads) >= num_leads:
                    print(f"✅ Found {len(leads)} leads after {attempts} domain attempts")
                    return leads
            
            # Show results for this domain
            if contacts:
                print(f"✅ Domain {domain}: Found {len(contacts)} leads")
            else:
                print(f"❌ Domain {domain}: No leads found")
            
        except Exception as e:
            print(f"⚠️  Error processing domain {domain}: {e}")
            # Continue to next domain instead of failing completely
            continue

    print(f" Final result: {len(leads)} leads found after {attempts} domain attempts")
    return leads[:num_leads]     # may be fewer if Firecrawl found less

def check_existing_contact(email: str) -> bool:
    """Check if a contact with this email already exists in the leads pool."""
    try:
        from Leadpoet.base.utils.pool import get_leads_from_pool
        
        # Get all leads from pool to check for duplicates
        all_leads = get_leads_from_pool(1000000)  # Get a large number to check all
        
        existing_emails = {lead.get("owner_email", "").lower() for lead in all_leads}
        return email.lower() in existing_emails
        
    except Exception as e:
        print(f"❌ Error checking existing contact: {e}")
        return False

# ---------------- Legacy Functions (Keep Original Interface) ----------------
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
    industry_scores = {industry: 0 for industry in INDUSTRY_KEYWORDS}
    for industry, keywords in INDUSTRY_KEYWORDS.items():
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

# ---------------- Main Function (Keep Original Interface) ----------------
async def get_leads(num_leads: int, industry: str = None, region: str = None) -> list:
    bt.logging.debug(f"Generating {num_leads} leads, industry={industry}, region={region}")

    # ------------------------------------------------------------------
    # 1) Try the Firecrawl sourcing pipeline first
    # ------------------------------------------------------------------
    print("🚀 Trying Firecrawl sourcing model...", flush=True)
    
    # Check if Firecrawl is available BEFORE trying to use it
    if not FIRECRAWL_AVAILABLE:
        print("⚠️  Firecrawl not available - no API key")
        print("❌ Cannot source leads without Firecrawl API key")
        print("🔄 Returning empty list - no sourcing possible")
        return []
    
    try:
        fc_leads = await get_firecrawl_leads(num_leads, industry, region)
        if len(fc_leads) >= num_leads:
            print(f"✅ Firecrawl sourcing model produced {len(fc_leads)} leads", flush=True)
            bt.logging.info(f"Firecrawl sourcing produced {len(fc_leads)} leads")
            return fc_leads
        else:
            print(f"ℹ️  Firecrawl produced {len(fc_leads)} leads; topping up with legacy database",
                  flush=True)
            bt.logging.info(f"Firecrawl produced only {len(fc_leads)} leads, topping up")
            
            # SHOW THE FIRECRAWL LEADS THAT WERE FOUND
            if fc_leads:
                print(f"📋 Firecrawl leads found:")
                for i, lead in enumerate(fc_leads, 1):
                    business = lead.get('Business', 'Unknown')
                    owner = lead.get('Owner Full name', 'Unknown')
                    email = lead.get('Owner(s) Email', 'No email')
                    print(f"  {i}. {business} - {owner} ({email})")
                print()
    except Exception as e:
        # Only fall back to legacy if there's a critical error (API keys missing, etc.)
        # Not if individual domains fail
        if "api_key_missing" in str(e) or "not available" in str(e):
            print(f"⚠️  Critical error with Firecrawl sourcing ({e}); defaulting to legacy database", flush=True)
            bt.logging.warning(f"Critical Firecrawl error: {e}")
        else:
            print(f"⚠️  Non-critical error with Firecrawl sourcing ({e}); continuing with available leads", flush=True)
            bt.logging.warning(f"Non-critical Firecrawl error: {e}")
            # Return whatever leads we have instead of falling back
            if 'fc_leads' in locals() and fc_leads:
                return fc_leads

    # ------------------------------------------------------------------
    # 2) Legacy JSON fallback (only if Firecrawl completely failed)
    # ------------------------------------------------------------------
    leads = []

    # If we have Firecrawl leads, start with those
    if 'fc_leads' in locals() and fc_leads:
        leads.extend(fc_leads)
        print(f"📥 Added {len(fc_leads)} Firecrawl leads to total")

    # Only use legacy if we have no Firecrawl leads at all
    if not leads:
        print("ℹ️  No Firecrawl leads available, using legacy database...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(COMPANY_LIST_URL, timeout=30) as response:
                response.raise_for_status()
                businesses = safe_json_load(await response.text())
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
        if region and business_region.lower() != business_region.lower():
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
    
    # REMOVED: No more fallback lead generation
    # The function now returns whatever leads were actually found, even if fewer than requested
    
    bt.logging.debug(f"Generated {len(leads)} leads")
    return leads

# ---------------- Legacy Industry Keywords (Keep Original Interface) ----------------
industry_keywords = {
    "Tech & AI": ["saas", "software", "cloud", "subscription", "platform", "app", "online", "hosted", "crm", "erp", "ai", "machine learning", "artificial intelligence", "automation", "data", "analytics", "predictive", "nlp", "robotics", "algorithm", "cybersecurity", "security", "privacy", "protection", "encryption", "firewall", "antivirus", "network", "tech", "digital", "systems", "computing", "internet", "agent", "API"],
    "Finance & Fintech": ["fintech", "finance", "bank", "payment", "blockchain", "crypto", "lending", "investment", "money", "trading", "wallet", "transaction", "capital", "insurance", "wealth", "loan", "budget", "financial", "accounting", "stock", "exchange", "credit", "debit", "ledger", "token", "currency", "remittance", "crowdfunding", "mortgage", "billing", "payroll", "audit", "web3", "crypto", "blockchain", "decentralized"],
    "Health & Wellness": ["health", "medical", "biotech", "pharma", "hospital", "clinic", "care", "wellness", "patient", "diagnostics", "genomics", "therapy", "medicine", "drug", "research", "clinical", "biology", "edtech", "education", "learning", "training", "e-learning", "course", "lms", "tutor", "study", "telemedicine", "fitness", "nutrition", "surgery", "rehabilitation", "genetics", "skincare", "skin"],
    "Media & Education": ["media", "entertainment", "streaming", "content", "gaming", "advertising", "digital media", "video", "music", "broadcast", "film", "television", "radio", "publishing", "news", "marketing", "social", "production", "animation", "vr", "ar", "podcast", "event", "promotion", "creative", "design", "journalism", "cinema", "theater", "branding", "influencer", "engagement", "education", "edu"],
    "Energy & Industry": ["energy", "sustainability", "renewable", "clean tech", "green", "environment", "solar", "wind", "carbon", "eco-friendly", "manufacturing", "supply chain", "logistics", "production", "b2b", "industrial", "warehouse", "assembly", "shipping", "factory", "engineering", "infrastructure", "mining", "agriculture", "construction", "materials", "transport", "power", "recycling", "automation", "machinery", "distribution"]
}

VALID_INDUSTRIES = list(industry_keywords.keys())

# ---------------- Main Entry Point (Keep Original Interface) ----------------
if __name__ == "__main__":

    async def miner():
        """Main mining function with graceful shutdown."""
        global shutdown_requested, current_batch, outbox_file
        
        print("DEBUG: Miner function started")  # Debug output
        
        try:
            print("DEBUG: Starting web server")  # Debug output
            # Start web server
            web_runner = await start_web_server()
            
            print("DEBUG: Web server started, initializing metrics")  # Debug output
            # Initialize metrics
            BATCH_SIZE_GAUGE.set(ARGS.batch_size)
            FLUSH_SEC_GAUGE.set(ARGS.flush_sec)
            
            # Check file locking availability
            try:
                import fcntl
                logger.info("file_locking_available")
            except ImportError:
                logger.warning("file_locking_not_available_windows", 
                              note="Multiple processes may cause race conditions")
            
            print("DEBUG: Loading domains")  # Debug output
            logger.info("miner_started", 
                       domains_file=ARGS.domains,
                       outbox_file="outbox/leads.jsonl",
                       pending_enrich_dir="outbox/pending_enrich")
            
            # Load domains
            domains = load_domains(ARGS.domains)
            print(f"DEBUG: Loaded {len(domains)} domains: {domains}")  # Debug output
            
            # Initialize outbox
            outbox_file = open("outbox/leads.jsonl", "a", encoding="utf-8")
            
            print("DEBUG: Processing domains")  # Debug output
            # Process domains
            for domain in domains:
                if shutdown_requested:
                    logger.info("shutdown_requested_processing_domain", domain=domain)
                    break
                    
                print(f"DEBUG: Processing domain: {domain}")  # Debug output
                try:
                    await process_domain(domain)
                except Exception as e:
                    logger.error("domain_processing_failed", domain=domain, error=str(e))
                    continue
            
            # Graceful shutdown: flush any remaining batch
            if current_batch and not shutdown_requested:
                logger.info("flushing_final_batch", batch_size=len(current_batch))
                await flush_batch(current_batch)
            
            print("DEBUG: Miner completed successfully")  # Debug output
            
        except Exception as e:
            print(f"DEBUG: Miner failed with error: {e}")  # Debug output
            logger.error("miner_failed", error=str(e))
            raise
        finally:
            # Cleanup
            if outbox_file:
                outbox_file.close()
                logger.info("outbox_file_closed")
            
            if 'web_runner' in locals():
                await web_runner.cleanup()
                logger.info("web_server_stopped")
            
            if shutdown_requested:
                logger.info("miner_shutdown_complete")
            else:
                logger.info("miner_completed")

    # Import missing functions that were referenced
    from prometheus_client import Counter, Histogram, start_http_server, Gauge
    import aiohttp
    from aiohttp import web
    
    # Define missing functions that were referenced in the original main
    async def start_web_server():
        """Start aiohttp web server for health and metrics."""
        app = web.Application()
        app.router.add_get('/healthz', lambda r: web.Response(text="OK", status=200))
        app.router.add_get('/metrics', lambda r: web.Response(text="", status=200))
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 9100)
        await site.start()
        
        logger.info("web_server_started", port=9100)
        return runner

    async def process_domain(domain: str):
        """Process a single domain with the new flow: extract → pre-enrich → enrich → final storage."""
        global current_batch
        
        logger.info("processing_domain_started", domain=domain)
        
        # Check if recently scraped
        if is_recently_scraped(domain):
            logger.info("domain_skipped_recent", domain=domain)
            return
        
        # Update timestamp
        update_last_scraped(domain)
        logger.info("domain_timestamp_updated", domain=domain)
        
        # Crawl domain
        logger.info("crawl_started", domain=domain)
        crawl_data = await crawl_domain(domain)
        if not crawl_data:
            logger.warning("crawl_failed", domain=domain)
            return
        
        logger.info("crawl_completed", domain=domain, pages_crawled=len(crawl_data))
        
        # Extract eligible URLs
        eligible_urls = []
        for page in crawl_data:
            if "metadata" in page and "sourceURL" in page["metadata"]:
                url = page["metadata"]["sourceURL"]
                if should_include_url(url) and not should_exclude_url(url):
                    eligible_urls.append(url)
        
        logger.info("eligible_urls_found", domain=domain, count=len(eligible_urls), urls=eligible_urls)
        
        if not eligible_urls:
            logger.info("no_eligible_urls", domain=domain)
            return
        
        # Extract contacts from URLs
        logger.info("extraction_started", domain=domain, url_count=len(eligible_urls))
        contacts = await extract_contacts_from_urls(eligible_urls, domain)
        logger.info("extraction_completed", 
                   domain=domain, 
                   contacts_found=len(contacts),
                   emails=[contact.get("email", "") for contact in contacts])
        
        # Filter legitimate emails
        legitimate_contacts = []
        for contact in contacts:
            email = contact.get("email", "")
            if email and await is_legit(email):
                legitimate_contacts.append(contact)
        
        logger.info("legitimacy_check_completed", 
                   domain=domain, 
                   total_contacts=len(contacts),
                   legitimate_contacts=len(legitimate_contacts),
                   legitimate_emails=[contact.get("email", "") for contact in legitimate_contacts])
        
        if not legitimate_contacts:
            logger.info("no_legitimate_contacts", domain=domain)
            return
        
        # Store leads in pre-enrich stage
        logger.info("pre_enrich_stage_started", domain=domain, lead_count=len(legitimate_contacts))
        await submit_pre_enrich(legitimate_contacts)
        logger.info("pre_enrich_stage_completed", domain=domain)
        
        # Enrich leads
        logger.info("enrichment_started", domain=domain, lead_count=len(legitimate_contacts))
        async with aiohttp.ClientSession() as session:
            enriched_contacts = await enrich_batch(session, legitimate_contacts)
        logger.info("enrichment_completed", 
                   domain=domain, 
                   enriched_count=len(enriched_contacts),
                   enrichment_stats={
                       "pdl_success": len([c for c in enriched_contacts if c.get("enrichment", {}).get("pdl")]),
                       "hunter_success": len([c for c in enriched_contacts if c.get("enrichment", {}).get("hunter")]),
                       "no_enrichment": len([c for c in enriched_contacts if not c.get("enrichment", {})])
                   })
        
        # Store enriched leads in final outbox
        logger.info("final_storage_stage_started", domain=domain, lead_count=len(enriched_contacts))
        await submit_enriched(enriched_contacts)
        logger.info("final_storage_stage_completed", domain=domain)
        
        logger.info("domain_processing_completed", 
                   domain=domain,
                   total_leads_processed=len(enriched_contacts))

    async def submit_pre_enrich(batch: List[Dict[str, Any]]):
        """Submit pre-enrich leads to pending_enrich directory."""
        if not batch:
            return
        
        try:
            # Create pending_enrich directory if it doesn't exist
            Path("outbox/pending_enrich").mkdir(parents=True, exist_ok=True)
            
            # Get current date for filename
            date_str = datetime.utcnow().strftime("%Y%m%d")
            pending_file = Path("outbox/pending_enrich") / f"{date_str}.jsonl"
            
            # Log pre-enrich storage start
            logger.info("pre_enrich_storage_started", 
                       batch_size=len(batch), 
                       file=str(pending_file),
                       emails=[lead.get("email", "") for lead in batch])
            
            # Write leads to pending file
            with open(pending_file, "a", encoding="utf-8") as f:
                for lead in batch:
                    json_line = json.dumps(lead, ensure_ascii=False) + "\n"
                    f.write(json_line)
            
            logger.info("leads_submitted_pre_enrich", 
                       count=len(batch), 
                       file=str(pending_file),
                       file_size=Path(pending_file).stat().st_size if Path(pending_file).exists() else 0)
            
        except Exception as e:
            logger.error("pre_enrich_submission_failed", error=str(e), batch_size=len(batch))
            raise

    async def submit_enriched(batch: List[Dict[str, Any]]):
        """Submit enriched leads to final outbox JSONL file."""
        if not batch:
            return
        
        try:
            # Log final storage start
            logger.info("final_storage_started", 
                       batch_size=len(batch),
                       emails=[lead.get("email", "") for lead in batch])
            
            for lead in batch:
                json_line = json.dumps(lead, ensure_ascii=False) + "\n"
                outbox_file.write(json_line)
                outbox_file.flush()  # Ensure immediate write
            
            logger.info("leads_submitted_final", 
                       count=len(batch),
                       outbox_file="outbox/leads.jsonl",
                       enrichment_summary={
                           "with_pdl": len([l for l in batch if l.get("enrichment", {}).get("pdl")]),
                           "with_hunter": len([l for l in batch if l.get("enrichment", {}).get("hunter")]),
                           "no_enrichment": len([l for l in batch if not l.get("enrichment", {})])
                       })
            
        except Exception as e:
            logger.error("final_submission_failed", error=str(e), batch_size=len(batch))
            raise

    async def flush_batch(batch: List[Dict[str, Any]]):
        """Flush a batch of leads with pre-enrich storage, enrichment, and final submission."""
        if not batch:
            return
        
        logger.info("batch_flush_started", 
                   batch_size=len(batch),
                   emails=[lead.get("email", "") for lead in batch])
        
        # Step 1: Store leads in pre-enrich state
        logger.info("batch_pre_enrich_started", batch_size=len(batch))
        await submit_pre_enrich(batch)
        logger.info("batch_pre_enrich_completed", batch_size=len(batch))
        
        # Step 2: Enrich the leads
        logger.info("batch_enrichment_started", batch_size=len(batch))
        async with aiohttp.ClientSession() as session:
            enriched_batch = await enrich_batch(session, batch)
        logger.info("batch_enrichment_completed", 
                   original_size=len(batch),
                   enriched_size=len(enriched_batch))
        
        # Step 3: Submit enriched leads to final destination
        logger.info("batch_final_storage_started", batch_size=len(enriched_batch))
        await submit_enriched(enriched_batch)
        logger.info("batch_final_storage_completed", batch_size=len(enriched_batch))
        
        logger.info("batch_flush_completed", 
                   original_size=len(batch),
                   final_size=len(enriched_batch))

    async def enrich_batch(session: aiohttp.ClientSession, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Enrich batch of leads with PDL and Hunter APIs."""
        if not batch:
            return batch
        
        logger.info("enriching_batch", batch_size=len(batch))
        ENRICH_CALLS_TOTAL.inc()
        
        # Check for duplicates using Redis
        leads_to_enrich = []
        for lead in batch:
            email = lead.get("email", "").lower().strip()
            if not email:
                continue
                
            is_duplicate = await check_redis_duplicate(email)
            if is_duplicate:
                logger.info("skipping_duplicate_email", email=email)
                continue
            
            leads_to_enrich.append(lead)
        
        if not leads_to_enrich:
            logger.info("no_leads_to_enrich_after_duplicate_check")
            return batch
        
        # Enrich each lead
        for lead in leads_to_enrich:
            email = lead.get("email", "").lower().strip()
            
            # Enrich with PDL first
            try:
                pdl_data = await enrich_with_pdl(session, email)
                if pdl_data:
                    lead["enrichment"]["pdl"] = pdl_data
            except Exception as e:
                logger.error("pdl_enrichment_failed", email=email, error=str(e))
            
            # Enrich with Hunter as fallback
            try:
                hunter_data = await enrich_with_hunter(session, email)
                if hunter_data:
                    lead["enrichment"]["hunter"] = hunter_data
            except Exception as e:
                logger.error("hunter_enrichment_failed", email=email, error=str(e))
            
            # Record latency
            start_time = time.time()
            await asyncio.sleep(0.1)
        
        logger.info("batch_enriched", enriched_count=len(leads_to_enrich))
        return batch

    async def check_redis_duplicate(email: str) -> bool:
        """Check if email is a duplicate using Redis."""
        try:
            # Initialize Redis client if URL is provided
            if os.getenv('REDIS_URL'):
                redis_client = redis.from_url(os.getenv('REDIS_URL'))
                email_hash = hashlib.sha256(email.lower().encode()).hexdigest()
                result = redis_client.set(f"email:{email_hash}", "1", ex=86400, nx=True)
                return not result  # True if key already existed (duplicate)
            else:
                return False
        except Exception as e:
            logger.error("redis_check_failed", email=email, error=str(e))
            return False

    async def enrich_with_pdl(session: aiohttp.ClientSession, email: str) -> Optional[Dict[str, Any]]:
        """Enrich email with PDL API using retry logic and gzip compression."""
        if not ARGS.pdl_key:
            return None
            
        try:
            payload = {"email": email}
            compressed_data = gzip.compress(json.dumps(payload).encode('utf-8'))
            
            async with session.post(
                "https://api.peopledatalabs.com/v5/person/enrich",
                params={"email": email},
                headers={
                    "X-Api-Key": ARGS.pdl_key,
                    "Content-Encoding": "gzip",
                    "Content-Type": "application/json"
                },
                data=compressed_data,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    ENRICH_CREDITS_TOTAL.inc()  # 1 credit per PDL call
                    return data
                else:
                    logger.warning("pdl_api_error", email=email, status=response.status)
                    return None
        except Exception as e:
            logger.error("pdl_enrichment_failed", email=email, error=str(e))
            raise

    async def enrich_with_hunter(session: aiohttp.ClientSession, email: str) -> Optional[Dict[str, Any]]:
        """Enrich email with Hunter API using retry logic."""
        try:
            async with session.get(
                "https://api.hunter.io/v2/email-verifier",
                params={"email": email, "api_key": ARGS.hunter_key},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    logger.warning("hunter_api_error", email=email, status=response.status)
                    return None
        except Exception as e:
            logger.error("hunter_enrichment_failed", email=email, error=str(e))
            raise

    # Run the miner
    asyncio.run(miner())