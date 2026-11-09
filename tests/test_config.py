import pytest
from pydantic import ValidationError

from app.config import Settings


def config(proxy):
    return Settings(
        _env_file=None,
        api_key="test-key-at-least-32-characters-long",
        telegram_proxy_url=proxy,
    )


def test_proxy_credentials_are_hidden():
    settings = config("socks5://client:private-password@proxy:1080")
    assert "private-password" not in repr(settings)
    assert "private-password" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "url",
    ["vless://private-password@proxy:443", "socks5://proxy:999999", "http://proxy:80/path"],
)
def test_invalid_proxy_does_not_leak_credentials(url):
    with pytest.raises(ValidationError) as error:
        config(url)
    assert "private-password" not in str(error.value)


def test_proxy_can_be_disabled():
    assert config("").telegram_proxy_url.get_secret_value() == ""
