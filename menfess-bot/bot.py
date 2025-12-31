"""
Bot utilities: HTTPX request wrapper with timeouts + semaphore and safe send wrappers for Telegram

This module provides:
- HTTPXRequest: a small helper around httpx.Client that enforces timeouts, retries, and limits
  concurrency with a semaphore to reduce httpx.ConnectTimeout errors.
- safe_send_* wrappers: helpers to call telegram.Bot send/edit/delete methods while catching
  telegram.error.TimedOut (and related transient errors) and retrying with exponential backoff.

These helpers are intentionally conservative: they retry a few times with backoff and log
failures rather than raising for every transient network hiccup.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Any, Optional, Dict

import httpx
import telegram
from telegram.error import TimedOut, NetworkError

logger = logging.getLogger(__name__)

# HTTPX configuration
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(timeout=None, connect=DEFAULT_CONNECT_TIMEOUT, read=DEFAULT_READ_TIMEOUT)
DEFAULT_HTTP_MAX_CONCURRENCY = 10
DEFAULT_HTTP_MAX_RETRIES = 3

# Telegram safe-send configuration
TELEGRAM_SEND_MAX_RETRIES = 3
TELEGRAM_SEND_BACKOFF_BASE = 1.5  # multiplier for exponential backoff


class HTTPXRequest:
    """Lightweight httpx wrapper with timeouts, retries and a semaphore to limit concurrency.

    Usage:
        http = HTTPXRequest()
        resp = http.get("https://example.com")

    The wrapper will:
    - limit concurrent requests using a threading.BoundedSemaphore
    - retry on ConnectTimeout/ReadTimeout up to max_retries with exponential backoff
    - surface other httpx.HTTPError exceptions
    """

    def __init__(
        self,
        timeout: Optional[httpx.Timeout] = None,
        max_concurrency: int = DEFAULT_HTTP_MAX_CONCURRENCY,
        max_retries: int = DEFAULT_HTTP_MAX_RETRIES,
    ) -> None:
        self.timeout = timeout or DEFAULT_HTTP_TIMEOUT
        self.client = httpx.Client(timeout=self.timeout)
        # BoundedSemaphore prevents unlimited growth and Thundering Herd issues
        self.semaphore = threading.BoundedSemaphore(max_concurrency)
        self.max_retries = max_retries

    def _acquire(self, wait_timeout: Optional[float] = None) -> bool:
        try:
            return self.semaphore.acquire(timeout=wait_timeout)
        except Exception:
            # As a fallback, try a blocking acquire
            try:
                self.semaphore.acquire()
                return True
            except Exception:
                return False

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make an HTTP request with retries and semaphore protection.

        Raises httpx.HTTPError (or subclass) on persistent failures.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            acquired = self._acquire(wait_timeout=10.0)
            if not acquired:
                logger.debug("HTTPXRequest: semaphore acquire timed out, waiting without timeout")
                # Attempt a blocking acquire if timed out
                self.semaphore.acquire()
            try:
                logger.debug("HTTPXRequest: %s %s (attempt %d/%d)", method.upper(), url, attempt, self.max_retries)
                resp = self.client.request(method, url, **kwargs)
                # Raise for 4xx/5xx so callers can decide to handle it
                resp.raise_for_status()
                return resp

            except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_exc = exc
                logger.warning(
                    "HTTPX timeout (%s) for %s %s — attempt %d/%d",
                    type(exc).__name__, method.upper(), url, attempt, self.max_retries,
                )
                # backoff before retrying
                if attempt < self.max_retries:
                    sleep_time = 2 ** (attempt - 1)
                    logger.debug("Sleeping for %s seconds before retry", sleep_time)
                    time.sleep(sleep_time)
                    continue
                else:
                    logger.exception("Exceeded retries for HTTPX timeout while requesting %s %s", method, url)
                    raise

            except httpx.HTTPError as exc:
                logger.exception("HTTPX error during request %s %s: %s", method.upper(), url, exc)
                # Non-timeout httpx errors — re-raise to let callers handle
                raise

            finally:
                # Ensure semaphore is released if we acquired it
                try:
                    self.semaphore.release()
                except ValueError:
                    # release called more times than acquire — shouldn't happen, but ignore
                    pass

        # If we exit the loop, raise the last exception
        assert last_exc is not None
        raise last_exc

    # convenience methods
    def get(self, url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, data=data, json=json, **kwargs)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


def _retry_on_timedout(func, max_retries: int = TELEGRAM_SEND_MAX_RETRIES, backoff_base: float = TELEGRAM_SEND_BACKOFF_BASE):
    """Internal helper to retry Telegram Bot methods on TimedOut/NetworkError.

    func should be a zero-argument callable that performs the network operation.
    """

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except TimedOut as exc:
            last_exc = exc
            logger.warning("Telegram TimedOut on attempt %d/%d: %s", attempt, max_retries, exc)
        except NetworkError as exc:
            # NetworkError is often transient; treat similarly
            last_exc = exc
            logger.warning("Telegram NetworkError on attempt %d/%d: %s", attempt, max_retries, exc)

        # only sleep/retry if we have more attempts left
        if attempt < max_retries:
            backoff = backoff_base ** attempt
            logger.debug("Retrying Telegram call in %.2f seconds (attempt %d)", backoff, attempt + 1)
            time.sleep(backoff)

    # Retries exhausted — re-raise the last exception for the caller to handle/log
    logger.exception("Telegram send failed after %d attempts", max_retries)
    raise last_exc


# Public safe send wrappers
def safe_send_message(bot: telegram.Bot, chat_id: int | str, text: str, **kwargs: Any) -> telegram.Message:
    """Safely send a message with retries on TimedOut/NetworkError."""

    def _call():
        return bot.send_message(chat_id=chat_id, text=text, **kwargs)

    return _retry_on_timedout(_call)


def safe_send_photo(bot: telegram.Bot, chat_id: int | str, photo: Any, **kwargs: Any) -> telegram.Message:
    """Safely send a photo with retries on TimedOut/NetworkError."""

    def _call():
        return bot.send_photo(chat_id=chat_id, photo=photo, **kwargs)

    return _retry_on_timedout(_call)


def safe_edit_message_text(bot: telegram.Bot, chat_id: int | str, message_id: int, text: str, **kwargs: Any) -> telegram.Message:
    """Safely edit a message's text with retries."""

    def _call():
        return bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)

    return _retry_on_timedout(_call)


def safe_delete_message(bot: telegram.Bot, chat_id: int | str, message_id: int) -> bool:
    """Safely delete a message with retries."""

    def _call():
        return bot.delete_message(chat_id=chat_id, message_id=message_id)

    return _retry_on_timedout(_call)


# Expose a module-level HTTPX helper for convenience
http = HTTPXRequest()


# Example: if other modules import this file for helpers, they can use `http.get(...)` and the safe_send_* wrappers.

if __name__ == "__main__":
    # Basic smoke test for the module (not exhaustive)
    logging.basicConfig(level=logging.DEBUG)
    try:
        r = http.get("https://httpbin.org/get")
        print("httpbin status:", r.status_code)
    finally:
        http.close()
