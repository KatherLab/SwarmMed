import subprocess
from django.core.cache import cache
import socket

def get_tailscale_ip():
    """Get the current machine's Tailscale IPv4 address."""
    # Cache the IP for 5 minutes to avoid repeated subprocess calls
    cached_ip = cache.get('tailscale_ip')
    if cached_ip:
        return cached_ip
    
    try:
        result = subprocess.run(['tailscale', 'ip', '--4'], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        ip = result.stdout.strip()
        cache.set('tailscale_ip', ip, 300)  # Cache for 5 minutes
        return ip
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "Not Available"

def get_local_ip():
    """Get the local IP address - simplified and reliable"""
    try:
        # Create a socket and connect to an external address
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connect to Cloudflare DNS (doesn't actually send data)
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        # Fallback to hostname method
        try:
            hostname = socket.gethostname()
            return socket.gethostbyname(hostname)
        except:
            return "127.0.0.1"
