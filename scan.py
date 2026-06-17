import socket
import concurrent.futures

def check_port(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(('10.0.0.16', p)) == 0:
            return p
    return None

with concurrent.futures.ThreadPoolExecutor(100) as executor:
    results = executor.map(check_port, range(1, 65535))
    for r in filter(None, results):
        print(f"Port {r} OPEN")
