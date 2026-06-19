"""Unit tests for Telegram Markdown escaping and config."""

from __future__ import annotations

from telegram_notifier import TelegramConfig, escape_markdown_v2


def test_escape_markdown_v2_special_chars() -> None:
    raw = "AAPL_BUY (100%) | $180.50"
    escaped = escape_markdown_v2(raw)
    assert "\\_" in escaped
    assert "\\(" in escaped
    assert "\\|" in escaped
    assert "\\$" in escaped


def test_config_disabled_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "true")
    cfg = TelegramConfig.from_env(bot_token="", chat_id="")
    assert cfg.enabled is False


def test_config_enabled_with_credentials(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "true")
    cfg = TelegramConfig.from_env(bot_token="test-token", chat_id="12345")
    assert cfg.enabled is True
    assert cfg.bot_token == "test-token"
    assert cfg.chat_id == "12345"


def main() -> int:
    test_escape_markdown_v2_special_chars()
    print("Run with pytest for full suite including monkeypatch tests.")
    print("ALL TELEGRAM NOTIFIER STATIC TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
