"""How many routers away a TCP connection to port 443 is answered.

    python tcp_hops.py ADDRESS MOST

A connection whose packets may cross only N routers succeeds when what
answers is N routers away or nearer, so the smallest N that connects is
the distance of whoever answers. It needs no special rights on Linux,
macOS or Windows. Prints that N, or 0 when nothing answers within MOST.
"""
import socket
import sys


def connects(address, ttl):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        s.settimeout(2)
        s.connect((address, 443))
        return True
    except OSError:
        return False
    finally:
        s.close()


def distance(address, most):
    if not connects(address, most):
        return 0
    low, high = 1, most
    while low < high:
        middle = (low + high) // 2
        if connects(address, middle):
            high = middle
        else:
            low = middle + 1
    return low


if __name__ == "__main__":
    print(distance(sys.argv[1], int(sys.argv[2])))
