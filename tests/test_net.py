import pytest

from caplog.net import client_ip, parse_trusted_proxies


def test_untrusted_ignores_forwarded_headers():
    nets = parse_trusted_proxies("10.0.0.8")
    assert client_ip("192.0.2.4", "1.2.3.4", "9.9.9.9", nets) == "192.0.2.4"


def test_trusted_uses_forwarded_for_then_real_ip():
    nets = parse_trusted_proxies("10.0.0.0/8")
    assert client_ip("10.1.1.1", "1.2.3.4, 5.6.7.8", "", nets) == "1.2.3.4"
    assert client_ip("10.1.1.1", "", "8.8.8.8", nets) == "8.8.8.8"


def test_bad_proxy_is_rejected():
    with pytest.raises(ValueError):
        parse_trusted_proxies("not-an-ip")
