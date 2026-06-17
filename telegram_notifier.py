"""
Asynchronous Telegram notification layer for live trading reports.

Credentials are loaded from environment variables (see `.env.example`).
Never commit real bot tokens to version control.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter

load_dotenv(override=True)

logger = logging.getLogger(__name__)

AlertLevel = Literal["CRITICAL", "WARNING", "INFO"]
TradeAction = Literal["BUY", "SELL", "HOLD", "PENDING"]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_MARKDOWN_V2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!$\\])")


def escape_markdown_v2(text: str) -> str:
    """
    Escape dynamic text for Telegram MarkdownV2 parse mode.

    Prevents API 400 errors when user or market data contains reserved chars.
    """
    if text is None:
        return ""
    return _MARKDOWN_V2_SPECIAL.sub(r"\\\1", str(text))


def _normalize_chat_id(chat_id: str) -> str | int:
    """Strip wrappers and coerce numeric IDs (private/group chats)."""
    cleaned = str(chat_id).strip().strip('"').strip("'")
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return cleaned


@dataclass(frozen=True)
class TelegramConfig:
    """Runtime configuration for the Telegram notifier."""

    bot_token: str
    chat_id: str
    max_retries: int = 3
    base_backoff_seconds: float = 1.0
    parse_mode: str = ParseMode.MARKDOWN_V2
    enabled: bool = True

    @classmethod
    def from_env(
        cls,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        enabled: bool | None = None,
    ) -> TelegramConfig:
        token = bot_token if bot_token is not None else TELEGRAM_BOT_TOKEN
        chat = chat_id if chat_id is not None else TELEGRAM_CHAT_ID
        if enabled is None:
            enabled = os.getenv("USE_TELEGRAM_ALERTS", "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        return cls(
            bot_token=str(token).strip(),
            chat_id=str(chat).strip().strip('"').strip("'"),
            max_retries=int(os.getenv("TELEGRAM_MAX_RETRIES", "3")),
            base_backoff_seconds=float(os.getenv("TELEGRAM_BACKOFF_SECONDS", "1.0")),
            enabled=enabled and bool(str(token).strip()) and bool(str(chat).strip()),
        )


async def _send_message_safe(
    text: str,
    *,
    config: TelegramConfig | None = None,
) -> Any:
    """
    Send one MarkdownV2 message using a fresh Bot session per dispatch.

    Each call opens and closes its own HTTP session via ``async with Bot(...)``.
    """
    cfg = config or TelegramConfig.from_env()
    if not cfg.enabled:
        logger.debug("Telegram alerts disabled — message skipped")
        return None

    token = cfg.bot_token or TELEGRAM_BOT_TOKEN
    chat_id = _normalize_chat_id(cfg.chat_id or TELEGRAM_CHAT_ID)

    if not token or chat_id in ("", None):
        logger.error("Telegram credentials missing.")
        return None

    max_retries = max(cfg.max_retries, 1)

    async with Bot(token=token) as bot:
        for attempt in range(max_retries):
            try:
                message = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                )
                logger.info(
                    "Telegram message sent successfully (chat_id=%s, message_id=%s).",
                    chat_id,
                    message.message_id,
                )
                return message
            except RetryAfter as exc:
                wait_seconds = max(float(exc.retry_after), 2**attempt)
                logger.warning("Attempt %s failed (rate limit): %s", attempt + 1, exc)
                if attempt >= max_retries - 1:
                    logger.error(
                        "Failed to send Telegram message after %s attempts: %s",
                        max_retries,
                        exc,
                    )
                    return None
                await asyncio.sleep(wait_seconds)
            except Exception as exc:
                logger.warning("Attempt %s failed: %s", attempt + 1, exc)
                if attempt >= max_retries - 1:
                    logger.error(
                        "Failed to send Telegram message after %s attempts: %s",
                        max_retries,
                        exc,
                    )
                    return None
                await asyncio.sleep(2**attempt)

    return None


def _format_trade_report_text(
    ticker: str,
    action: TradeAction | str,
    quantity: int,
    price: float,
    execution_time: datetime | str,
) -> str:
    action_upper = str(action).upper()
    emoji = {
        "BUY": "🟢",
        "SELL": "🔴",
        "HOLD": "⚪",
        "PENDING": "🟡",
    }.get(action_upper, "📌")

    if isinstance(execution_time, datetime):
        time_text = execution_time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        time_text = str(execution_time)

    ticker_e = escape_markdown_v2(ticker.upper())
    action_e = escape_markdown_v2(action_upper)
    qty_e = escape_markdown_v2(str(int(quantity)))
    price_e = escape_markdown_v2(f"{float(price):,.2f}")
    time_e = escape_markdown_v2(time_text)

    return (
        f"{emoji} *\\[{action_e}\\]* "
        f"{qty_e} *{ticker_e}* @ ${price_e}\n"
        f"🕒 `{time_e}`"
    )


def _format_daily_summary_text(
    total_pnl: float,
    win_rate: float,
    total_trades: int,
    open_positions: int,
    *,
    as_of: datetime | None = None,
) -> str:
    stamp = (as_of or datetime.now()).strftime("%Y-%m-%d")
    pnl_sign = "+" if total_pnl >= 0 else ""
    date_e = escape_markdown_v2(stamp)
    pnl_e = escape_markdown_v2(f"{pnl_sign}{total_pnl:,.2f}")
    win_e = escape_markdown_v2(f"{win_rate:.1f}")
    trades_e = escape_markdown_v2(str(int(total_trades)))
    open_e = escape_markdown_v2(str(int(open_positions)))

    return (
        f"📊 *Daily Trading Summary* \\({date_e}\\)\n"
        f"Total PnL: ${pnl_e}\n"
        f"Win Rate: {win_e}%\n"
        f"Total Trades: {trades_e}\n"
        f"Open Positions: {open_e}"
    )


def _format_system_alert_text(level: AlertLevel | str, message: str) -> str:
    level_upper = str(level).upper()
    emoji = {
        "CRITICAL": "🚨",
        "WARNING": "⚠️",
        "INFO": "ℹ️",
    }.get(level_upper, "📢")

    level_e = escape_markdown_v2(level_upper)
    message_e = escape_markdown_v2(message)
    time_e = escape_markdown_v2(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    return (
        f"{emoji} *\\[{level_e}\\]*\n"
        f"{message_e}\n"
        f"🕒 `{time_e}`"
    )


class TelegramNotifier:
    """Thin async facade; each send uses its own ``async with Bot`` session."""

    def __init__(self, config: TelegramConfig | None = None) -> None:
        self.config = config or TelegramConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def send_trade_report(
        self,
        ticker: str,
        action: TradeAction | str,
        quantity: int,
        price: float,
        execution_time: datetime | str,
    ) -> Any:
        text = _format_trade_report_text(
            ticker, action, quantity, price, execution_time
        )
        return await _send_message_safe(text, config=self.config)

    async def send_daily_summary(
        self,
        total_pnl: float,
        win_rate: float,
        total_trades: int,
        open_positions: int,
        *,
        as_of: datetime | None = None,
    ) -> Any:
        text = _format_daily_summary_text(
            total_pnl,
            win_rate,
            total_trades,
            open_positions,
            as_of=as_of,
        )
        return await _send_message_safe(text, config=self.config)

    async def send_system_alert(self, level: AlertLevel | str, message: str) -> Any:
        text = _format_system_alert_text(level, message)
        return await _send_message_safe(text, config=self.config)

    async def close(self) -> None:
        """No-op — Bot sessions are scoped per message send."""
        return None


async def send_trade_report(
    ticker: str,
    action: TradeAction | str,
    quantity: int,
    price: float,
    execution_time: datetime | str,
    *,
    config: TelegramConfig | None = None,
) -> Any:
    text = _format_trade_report_text(
        ticker, action, quantity, price, execution_time
    )
    return await _send_message_safe(text, config=config)


async def send_daily_summary(
    total_pnl: float,
    win_rate: float,
    total_trades: int,
    open_positions: int,
    *,
    config: TelegramConfig | None = None,
    as_of: datetime | None = None,
) -> Any:
    text = _format_daily_summary_text(
        total_pnl,
        win_rate,
        total_trades,
        open_positions,
        as_of=as_of,
    )
    return await _send_message_safe(text, config=config)


async def send_system_alert(
    level: AlertLevel | str,
    message: str,
    *,
    config: TelegramConfig | None = None,
) -> Any:
    text = _format_system_alert_text(level, message)
    return await _send_message_safe(text, config=config)


def run_telegram_sync(coro) -> Any:
    """Run an async Telegram coroutine from synchronous code."""
    try:
        return asyncio.run(coro)
    except Exception as exc:
        logger.error("Telegram dispatch failed: %s", exc)
        return None


def diagnose_telegram_setup() -> int:
    """
    Read-only Telegram API checks: bot identity, recent chats, env chat_id match.

    Run: python telegram_notifier.py --diagnose
    """
    import json
    import urllib.error
    import urllib.request

    config = TelegramConfig.from_env()
    token = config.bot_token
    env_chat_id = _normalize_chat_id(config.chat_id) if config.chat_id else None

    print("=" * 72)
    print("Telegram setup diagnosis")
    print("=" * 72)
    print(f"USE_TELEGRAM_ALERTS : {os.getenv('USE_TELEGRAM_ALERTS', 'false')}")
    print(f"Config enabled      : {config.enabled}")
    print(f"Token present       : {bool(token)}")
    print(f"TELEGRAM_CHAT_ID    : {env_chat_id!r}")
    print()

    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing from .env")
        return 1

    def _api(method: str) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"HTTP {exc.code} calling {method}: {body}")
            return {"ok": False, "error_code": exc.code, "description": body}

    me = _api("getMe")
    if not me.get("ok"):
        print("ERROR: Token invalid or revoked. Create a new token via @BotFather.")
        return 1

    bot_user = me["result"]
    username = bot_user.get("username", "")
    print(f"Bot username        : @{username}")
    print(f"Bot id              : {bot_user.get('id')}")

    webhook = _api("getWebhookInfo")
    if webhook.get("ok"):
        hook_url = webhook.get("result", {}).get("url") or ""
        if hook_url:
            print(f"Webhook set         : YES ({hook_url})")
            print("  getUpdates will be empty while a webhook is active.")
        else:
            print("Webhook set         : no")
    print()
    print("Step 1 - Open this exact bot in Telegram and send /start:")
    print(f"  https://t.me/{username}")
    print()

    updates = _api("getUpdates?limit=20")
    if not updates.get("ok"):
        return 1

    seen_chat_ids: set[str | int] = set()
    print("Recent chats that messaged THIS bot (from getUpdates):")
    if not updates.get("result"):
        print("  (none — you have not sent /start to this bot yet, or a webhook is set)")
        print()
        print("Fix:")
        print(f"  1. Open https://t.me/{username}")
        print("  2. Tap Start and send /start")
        print("  3. Re-run: python telegram_notifier.py --diagnose")
        return 1

    for item in updates["result"]:
        message = (
            item.get("message")
            or item.get("edited_message")
            or item.get("channel_post")
            or {}
        )
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        seen_chat_ids.add(chat_id)
        print(
            f"  chat_id={chat_id} type={chat.get('type')} "
            f"title={chat.get('title') or chat.get('first_name', '')!r} "
            f"from_user_id={sender.get('id')} text={message.get('text', '')!r}"
        )

    print()
    if env_chat_id is not None and env_chat_id not in seen_chat_ids:
        print("MISMATCH: TELEGRAM_CHAT_ID is not among chats that contacted this bot.")
        print(f"  .env has {env_chat_id!r}")
        print(f"  bot has seen  {sorted(seen_chat_ids)!r}")
        print()
        if len(seen_chat_ids) == 1:
            suggested = next(iter(seen_chat_ids))
            print(f"Update .env to: TELEGRAM_CHAT_ID={suggested}")
        else:
            print("Pick the chat_id for your personal account from the list above.")
        return 1

    print("OK: TELEGRAM_CHAT_ID matches a chat that has messaged this bot.")
    print("If send still fails, revoke the token in @BotFather, update .env, /start again.")
    return 0


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def _demo_run(verbose: bool = False) -> None:
    """Send sample messages — useful for integration testing."""
    _configure_logging(verbose)
    config = TelegramConfig.from_env()

    if not config.enabled:
        logger.error(
            "Telegram disabled. Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "
            "and USE_TELEGRAM_ALERTS=true in .env"
        )
        return

    await send_trade_report(
        "AAPL",
        "BUY",
        10,
        180.50,
        datetime.now(),
        config=config,
    )
    await send_daily_summary(
        total_pnl=1250.75,
        win_rate=62.5,
        total_trades=8,
        open_positions=3,
        config=config,
    )
    await send_system_alert(
        "INFO",
        "Toss Trading Bot notifier online — test ping successful.",
        config=config,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Telegram trading notifier demo")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Check bot token, /start status, and TELEGRAM_CHAT_ID match",
    )
    args = parser.parse_args()

    if args.diagnose:
        raise SystemExit(diagnose_telegram_setup())

    asyncio.run(_demo_run(verbose=args.verbose))


if __name__ == "__main__":
    main()
