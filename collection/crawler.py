import re
import requests
from urllib.parse import urlparse, urljoin
import logging

logger = logging.getLogger(__name__)

def crawl_url(start_url, max_pages=50):
    """
    Simple crawler to find internal links (GET endpoints) starting from start_url.
    Returns a list of dicts: [{"method": "GET", "path": "/foo", "name": "Title or URL"}]
    """
    
    # Regex to extract href attributes
    href_pattern = re.compile(r'href=["\'](.*?)["\']', re.IGNORECASE)
    
    parsed_start = urlparse(start_url)
    base_domain = parsed_start.netloc
    scheme = parsed_start.scheme

    visited = set()
    to_visit = {start_url}
    found_endpoints = []

    # Safety limit
    count = 0

    while to_visit and count < max_pages:
        url = to_visit.pop()
        if url in visited:
            continue

        visited.add(url)
        count += 1
        
        try:
            resp = requests.get(url, timeout=5)
            # Only parse HTML responses
            content_type = resp.headers.get("Content-Type", "")
            
            # If it's HTML, look for links
            if "text/html" in content_type:
                logger.debug(f"Crawling {url}")
                page_text = resp.text
                links = href_pattern.findall(page_text)
                
                for link in links:
                    # Resolve relative URLs
                    full_link = urljoin(url, link)
                    parsed_link = urlparse(full_link)
                    
                    # Normalize to scheme+netloc+path for crawling logic
                    if parsed_link.netloc == base_domain or parsed_link.netloc == "":
                        if full_link not in visited:
                            to_visit.add(full_link)

            # Record discovered endpoint
            path = urlparse(url).path
            if not path: 
                path = "/"
            
            found_endpoints.append({
                "method": "GET",
                "path": path,
                "name": f"Page: {path}",
                "description": f"Crawled from {url}",
                "full_url": url
            })

        except requests.RequestException as e:
            logger.warning(f"Failed to crawl {url}: {e}")
            continue
            
    # Deduplicate results by (method, path)
    unique_map = {}
    for ep in found_endpoints:
        key = (ep["method"], ep["path"])
        if key not in unique_map:
            unique_map[key] = ep
            
    return list(unique_map.values())
