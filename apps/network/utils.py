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

def is_tailscale_connected():
    """Check if Tailscale is currently connected."""
    # Cache the connection state for 30 seconds
    cached_state = cache.get('tailscale_connected')
    if cached_state is not None:
        return cached_state
    
    try:
        result = subprocess.run(['tailscale', 'status'], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        
        # Check if the output indicates we're connected
        # Tailscale status returns exit code 0 and shows peer info when connected
        is_connected = bool(result.stdout.strip()) and 'peerapi' not in result.stdout.lower()
        
        cache.set('tailscale_connected', is_connected, 30)  # Cache for 30 seconds
        return "connected" if is_connected else "disconnected"

    except (subprocess.CalledProcessError, FileNotFoundError):
        cache.set('tailscale_connected', False, 10)
        return "disconnected"