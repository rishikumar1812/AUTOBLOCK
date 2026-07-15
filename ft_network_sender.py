"""
ft_network_sender.py  —  FT PC
Sends STOP and HELLO signals to Main PC FT listener (port 8998).

STOP payload:
  {
    "command":  "STOP",
    "source":   "FT",
    "ft_id":    "F1",
    "rack":     "front",
    "function": "Function 1"
  }

HELLO payload:
  {
    "command": "HELLO",
    "source":  "FT",
    "ft_id":   "F1"
  }
"""

import json
import socket
import logging

from ft_config_loader import (
    ft_id, ft_rack, ft_function_label,
    main_pc_ip, main_pc_port, timeout_sec,
    ft_display_label
)

logger = logging.getLogger(__name__)


def send_stop_signal() -> bool:
    """
    Send STOP signal to Main PC for this FT PC.
    Returns True if Main PC acknowledged OK, False otherwise.
    """
    ip      = main_pc_ip()
    port    = main_pc_port()
    timeout = timeout_sec()
    label   = ft_display_label()

    payload = json.dumps({
        "command":  "STOP",
        "source":   "FT",
        "ft_id":    ft_id(),
        "rack":     ft_rack(),
        "function": ft_function_label(),
    }).encode("utf-8")

    logger.info(
        f"[ft_sender] {label} → sending STOP to {ip}:{port} "
        f"(rack={ft_rack()}, function={ft_function_label()})"
    )

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
            sock.sendall(payload)
            raw      = sock.recv(1024)
            response = json.loads(raw.decode("utf-8"))
            if response.get("status") == "OK":
                logger.info(f"[ft_sender] {label} → Main PC acknowledged STOP")
                return True
            else:
                logger.error(
                    f"[ft_sender] {label} → Main PC error: "
                    f"{response.get('message', 'Unknown')}"
                )
                return False
    except socket.timeout:
        logger.error(
            f"[ft_sender] {label} → Timeout: Main PC {ip}:{port} "
            f"did not respond in {timeout}s"
        )
        return False
    except ConnectionRefusedError:
        logger.error(
            f"[ft_sender] {label} → Connection refused: "
            f"Is FT listener running on {ip}:{port}?"
        )
        return False
    except json.JSONDecodeError as e:
        logger.error(f"[ft_sender] {label} → Bad response: {e}")
        return False
    except Exception as e:
        logger.error(f"[ft_sender] {label} → Network error: {e}")
        return False


def send_hello() -> bool:
    """
    Send HELLO handshake to Main PC FT listener.
    Used by ft_dashboard.py for connection status check.
    Returns True if Main PC replied ACK.
    """
    ip      = main_pc_ip()
    port    = main_pc_port()

    payload = json.dumps({
        "command": "HELLO",
        "source":  "FT",
        "ft_id":   ft_id(),
    }).encode("utf-8")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect((ip, port))
            sock.sendall(payload)
            raw  = sock.recv(1024)
            resp = json.loads(raw.decode("utf-8"))
            return resp.get("status") == "ACK"
    except Exception:
        return False
