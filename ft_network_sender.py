"""
ft_network_sender.py  —  FT PC
Sends STOP signal to Main PC FT listener (port 8998).

Payload:
  {"command": "STOP", "source": "FT",
   "ft_number": 1, "ft_side": "front",
   "function": "Function 1", "setup_type": 8}

Main PC uses ft_number + ft_side + setup_type to map to the
correct Function row on the InLine_Pro HMI screen.
"""

import json
import socket
import logging

from ft_config_loader import (
    main_pc_ip, main_pc_port, ft_number,
    ft_side, ft_function_name, setup_type, ft_label
)

logger = logging.getLogger(__name__)

TIMEOUT_SEC = 10


def send_stop_signal() -> bool:
    """
    Send STOP signal to Main PC for this FT PC's function.
    Returns True if Main PC acknowledged OK, False otherwise.
    """
    ip   = main_pc_ip()
    port = main_pc_port()

    payload = json.dumps({
        "command":    "STOP",
        "source":     "FT",
        "ft_number":  ft_number(),
        "ft_side":    ft_side(),
        "function":   ft_function_name(),
        "setup_type": setup_type(),
    }).encode("utf-8")

    label = ft_label()
    logger.info(f"[ft_sender] {label} → sending STOP to {ip}:{port} "
                f"(function={ft_function_name()})")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT_SEC)
            sock.connect((ip, port))
            sock.sendall(payload)

            raw      = sock.recv(1024)
            response = json.loads(raw.decode("utf-8"))

            if response.get("status") == "OK":
                logger.info(f"[ft_sender] {label} → Main PC acknowledged STOP")
                return True
            else:
                logger.error(f"[ft_sender] {label} → Main PC error: "
                             f"{response.get('message', 'Unknown')}")
                return False

    except socket.timeout:
        logger.error(f"[ft_sender] {label} → Timeout: Main PC {ip}:{port} "
                     f"did not respond in {TIMEOUT_SEC}s")
        return False
    except ConnectionRefusedError:
        logger.error(f"[ft_sender] {label} → Connection refused: "
                     f"Is FT listener running on {ip}:{port}?")
        return False
    except json.JSONDecodeError as e:
        logger.error(f"[ft_sender] {label} → Bad response from Main PC: {e}")
        return False
    except Exception as e:
        logger.error(f"[ft_sender] {label} → Network error: {e}")
        return False


def send_hello() -> bool:
    """
    Send HELLO handshake to Main PC FT listener.
    Used by ft_dashboard.py for connection status check.
    """
    ip   = main_pc_ip()
    port = main_pc_port()
    payload = json.dumps({
        "command":   "HELLO",
        "source":    "FT",
        "ft_number": ft_number(),
        "ft_side":   ft_side(),
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