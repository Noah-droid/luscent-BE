"""
Security utilities for validating user-provided URLs and preventing SSRF attacks.
"""
import ipaddress
import socket
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)

# Private IP ranges that should never be accessible
BLOCKED_IP_RANGES = [
    '0.0.0.0/8',        # Current network (only valid as source)
    '10.0.0.0/8',       # Private network
    '100.64.0.0/10',    # Shared address space
    '127.0.0.0/8',      # Loopback
    '169.254.0.0/16',   # Link-local (AWS metadata service)
    '172.16.0.0/12',    # Private network
    '192.0.0.0/24',     # IETF Protocol Assignments
    '192.0.2.0/24',     # Documentation
    '192.168.0.0/16',   # Private network
    '198.18.0.0/15',    # Benchmarking
    '198.51.100.0/24',  # Documentation
    '203.0.113.0/24',   # Documentation
    '224.0.0.0/4',      # Multicast
    '240.0.0.0/4',      # Reserved
    '255.255.255.255/32', # Broadcast
    # IPv6 ranges
    '::1/128',          # Loopback
    'fe80::/10',        # Link-local
    'fc00::/7',         # Unique local
]

# Blocked schemes
BLOCKED_SCHEMES = ['file', 'ftp', 'gopher', 'data', 'javascript']

# Allowed schemes
ALLOWED_SCHEMES = ['http', 'https', 'ws', 'wss']



class URLSecurityError(Exception):
    """Raised when a URL fails security validation."""
    pass


def validate_url_security(url: str, allow_localhost: bool = False) -> tuple[bool, str]:
    """
    Validates a URL for security concerns (SSRF prevention).
    
    Args:
        url: The URL to validate
        allow_localhost: If True, allows localhost/127.0.0.1 (useful for development)
    
    Returns:
        Tuple of (is_valid, error_message)
        - (True, "") if URL is safe
        - (False, "reason") if URL is dangerous
    
    Raises:
        URLSecurityError: If URL is malicious
    """
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if not parsed.scheme:
            return False, "URL must include a scheme (http://, https://, ws://, or wss://)"
        
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return False, f"Scheme '{parsed.scheme}' is not allowed. Use http, https, ws, or wss."
        
        if parsed.scheme.lower() in BLOCKED_SCHEMES:
            return False, f"Scheme '{parsed.scheme}' is blocked for security reasons."
        
        # Check hostname exists
        if not parsed.hostname:
            return False, "URL must include a hostname"
        
        # Resolve hostname to IP
        try:
            ip_address = socket.gethostbyname(parsed.hostname)
        except socket.gaierror:
            return False, f"Cannot resolve hostname: {parsed.hostname}"
        except Exception as e:
            return False, f"DNS resolution error: {str(e)}"
        
        # Check if IP is in blocked ranges
        try:
            ip_obj = ipaddress.ip_address(ip_address)
            
            # Special handling for localhost in development
            if allow_localhost and ip_obj.is_loopback:
                logger.warning(f"Allowing localhost access (development mode): {url}")
                return True, ""
            
            # Check against blocked ranges
            for blocked_range in BLOCKED_IP_RANGES:
                network = ipaddress.ip_network(blocked_range)
                if ip_obj in network:
                    return False, f"URL resolves to blocked IP range: {ip_address} (in {blocked_range})"
            
            # Additional checks
            if ip_obj.is_private:
                return False, f"URL resolves to private IP address: {ip_address}"
            
            if ip_obj.is_reserved:
                return False, f"URL resolves to reserved IP address: {ip_address}"
            
            if ip_obj.is_multicast:
                return False, f"URL resolves to multicast IP address: {ip_address}"
            
        except ValueError as e:
            return False, f"Invalid IP address: {str(e)}"
        
        # All checks passed
        return True, ""
        
    except Exception as e:
        logger.error(f"URL validation error for {url}: {e}")
        return False, f"URL validation failed: {str(e)}"


def require_safe_url(url: str, allow_localhost: bool = False) -> None:
    """
    Validates URL and raises exception if unsafe.
    
    Args:
        url: The URL to validate
        allow_localhost: If True, allows localhost (for development)
    
    Raises:
        URLSecurityError: If URL is not safe
    """
    is_valid, error_message = validate_url_security(url, allow_localhost)
    if not is_valid:
        raise URLSecurityError(error_message)


def get_safe_url_or_none(url: str, allow_localhost: bool = False) -> str | None:
    """
    Returns the URL if safe, None otherwise.
    
    Args:
        url: The URL to validate
        allow_localhost: If True, allows localhost (for development)
    
    Returns:
        The URL if safe, None if dangerous
    """
    is_valid, _ = validate_url_security(url, allow_localhost)
    return url if is_valid else None
