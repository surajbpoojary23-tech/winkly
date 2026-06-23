#!/usr/bin/env python3
"""
Render deployment check script for Winkly Telegram bot.
This script verifies the deployment configuration before starting the bot.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

def check_environment():
    """Check if required environment variables are set."""
    required_vars = ['BOT_TOKEN']
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        return False
    
    print("✅ All required environment variables are set")
    return True

def check_port_binding():
    """Check if the bot will bind to the correct port."""
    port = os.getenv('PORT', '8080')
    try:
        port_num = int(port)
        if 1 <= port_num <= 65535:
            print(f"✅ Port {port} is valid")
            return True
        else:
            print(f"❌ Port {port} is out of valid range (1-65535)")
            return False
    except ValueError:
        print(f"❌ Port '{port}' is not a valid number")
        return False

def check_webhook_config():
    """Check webhook configuration."""
    webhook_url = os.getenv('WEBHOOK_URL')
    if webhook_url:
        print(f"✅ Webhook URL is configured: {webhook_url}")
        return True
    else:
        print("⚠️  WEBHOOK_URL not set - bot will use long-polling mode (may cause conflicts)")
        return False

def main():
    print("=== Winkly Bot Deployment Check ===\n")
    
    checks = [
        ("Environment Variables", check_environment),
        ("Port Binding", check_port_binding),
        ("Webhook Configuration", check_webhook_config),
    ]
    
    all_passed = True
    for check_name, check_func in checks:
        print(f"\n📋 Checking {check_name}...")
        if not check_func():
            all_passed = False
    
    print("\n=== Summary ===")
    if all_passed:
        print("✅ All checks passed!")
        sys.exit(0)
    else:
        print("❌ Some checks failed. Please fix the issues above.")
        sys.exit(1)

if __name__ == '__main__':
    main()
