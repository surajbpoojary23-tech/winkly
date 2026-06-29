#!/usr/bin/env python3
"""Deployment configuration check for the Winkly Telegram bot."""

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


def _present(name: str) -> bool:
    value = os.getenv(name)
    return bool(value and value.strip())


def check_required_environment() -> bool:
    required_vars = [
        'BOT_TOKEN',
        'REDIS_URL',
        'RAZORPAY_KEY_ID',
        'RAZORPAY_KEY_SECRET',
        'RAZORPAY_WEBHOOK_SECRET',
    ]
    if _present('WEBHOOK_URL'):
        required_vars.append('TELEGRAM_WEBHOOK_PATH')

    missing = [name for name in required_vars if not _present(name)]
    if missing:
        print(f"FAIL missing required environment variables: {', '.join(missing)}")
        return False

    print("OK required environment variables are set")
    return True


def check_port_binding() -> bool:
    port = os.getenv('PORT', '8080')
    try:
        port_num = int(port)
    except ValueError:
        print(f"FAIL PORT is not a number: {port}")
        return False

    if 1 <= port_num <= 65535:
        print(f"OK PORT is valid: {port_num}")
        return True

    print(f"FAIL PORT is out of range: {port_num}")
    return False


def check_webhook_config() -> bool:
    webhook_url = os.getenv('WEBHOOK_URL', '').strip()
    if not webhook_url:
        if os.getenv('ALLOW_LONG_POLLING', '').strip().lower() in {'1', 'true', 'yes'}:
            print("OK WEBHOOK_URL not set; long polling explicitly enabled")
            return True
        print("FAIL WEBHOOK_URL is required for deployment")
        print("Set ALLOW_LONG_POLLING=true only for local single-instance polling")
        return False

    parsed = urlparse(webhook_url)
    if parsed.scheme != 'https' or not parsed.netloc:
        print("FAIL WEBHOOK_URL must be a public HTTPS URL")
        return False

    secret = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
    if secret and len(secret) < 32:
        print("FAIL TELEGRAM_WEBHOOK_SECRET must be at least 32 characters")
        return False
    if not secret:
        print("OK TELEGRAM_WEBHOOK_SECRET not set; bot will derive one from BOT_TOKEN")
        return True

    print("OK webhook configuration is valid")
    return True


def main() -> int:
    print("=== Winkly Bot Deployment Check ===")
    checks = [
        check_required_environment,
        check_port_binding,
        check_webhook_config,
    ]
    ok = True
    for check in checks:
        ok = check() and ok

    if ok:
        print("PASS deployment checks passed")
        return 0

    print("FAIL deployment checks failed")
    return 1


if __name__ == '__main__':
    sys.exit(main())
