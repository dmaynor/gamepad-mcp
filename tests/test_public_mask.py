"""Public mode: IPv4 masking for log output (--public / GAMEPAD_PUBLIC).

The rule (David, 2026-11-21): X over the numerals of the IP address
except for the last octet, and the port number stays readable —
100.115.147.21:8765 becomes XXX.XXX.XXX.21:8765.
"""

import logging

from gamepad_mcp.server import PublicIPMaskFilter, mask_ips, public_log_config


def test_masks_all_but_last_octet():
    assert mask_ips("100.115.147.21") == "XXX.XXX.XXX.21"


def test_port_stays_readable():
    assert mask_ips("http://100.115.147.21:8765") == "http://XXX.XXX.XXX.21:8765"


def test_x_per_numeral():
    assert mask_ips("127.0.0.1") == "XXX.X.X.1"
    assert mask_ips("10.10.10.158") == "XX.XX.XX.158"


def test_masks_every_address_in_line():
    line = '100.105.169.113:51234 - "POST /mcp" via 100.115.147.21:8765'
    assert mask_ips(line) == 'XXX.XXX.XXX.113:51234 - "POST /mcp" via XXX.XXX.XXX.21:8765'


def test_text_without_ip_unchanged():
    assert mask_ips("Uvicorn running (Press CTRL+C to quit)") == \
        "Uvicorn running (Press CTRL+C to quit)"
    assert mask_ips("version 0.1.2") == "version 0.1.2"


def test_filter_rewrites_record_message():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1,
        "Uvicorn running on http://100.115.147.21:8765", (), None,
    )
    assert PublicIPMaskFilter().filter(record) is True
    assert record.getMessage() == "Uvicorn running on http://XXX.XXX.XXX.21:8765"


def test_filter_handles_format_args():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        "%s - request", ("100.105.169.113:51234",), None,
    )
    assert PublicIPMaskFilter().filter(record) is True
    assert record.getMessage() == "XXX.XXX.XXX.113:51234 - request"


def test_public_log_config_wires_filter_into_handlers():
    config = public_log_config()
    assert config["filters"]["public_ip"]["()"].endswith("PublicIPMaskFilter")
    assert config["handlers"], "uvicorn default config should have handlers"
    for handler in config["handlers"].values():
        assert "public_ip" in handler.get("filters", [])
