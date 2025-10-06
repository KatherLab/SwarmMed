import subprocess
from django.core.cache import cache
import socket

def get_tailscale_ip():
    """Get the current machine's Tailscale IPv4 address."""
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
    except PermissionError:
        return "Permission Denied"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "Not Available"

def is_tailscale_connected():
    """Check if Tailscale is currently connected."""
    cached_state = cache.get('tailscale_connected')
    if cached_state is not None:
        return cached_state
    
    try:
        result = subprocess.run(['tailscale', 'status'], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        
        is_connected = bool(result.stdout.strip()) and 'peerapi' not in result.stdout.lower()
        
        status = "connected" if is_connected else "disconnected"
        cache.set('tailscale_connected', status, 30)
        return status

    except PermissionError:
        cache.set('tailscale_connected', "Permission Denied", 10)
        return "Permission Denied"
    except (subprocess.CalledProcessError, FileNotFoundError):
        cache.set('tailscale_connected', "disconnected", 10)
        return "disconnected"

def get_hostname():
    """Get the current machine's hostname."""
    try:
        return socket.gethostname()
    except Exception:
        return "Not Available"