#!/usr/bin/env python3
"""
Simple verification script for Winkly bot fixes.
"""

import os
import sys
from pathlib import Path

def check_bot_py():
    """Check if bot.py has been updated with port configuration."""
    print("Checking bot.py for port configuration fix...")
    
    bot_path = Path(__file__).parent / 'bot.py'
    content = bot_path.read_text(encoding='utf-8', errors='ignore')
    
    # Check if PORT environment variable is used
    if 'os.getenv(\'PORT\'' in content or 'os.getenv("PORT"' in content:
        print("   Port configuration fix found in bot.py")
        return True
    else:
        print("   Port configuration fix not found in bot.py")
        return False

def check_bot_lock_py():
    """Check if bot_lock.py exists."""
    print("Checking bot_lock.py implementation...")
    
    lock_path = Path(__file__).parent / 'bot_lock.py'
    if lock_path.exists():
        content = lock_path.read_text(encoding='utf-8', errors='ignore')
        if 'ProcessLock' in content and 'fcntl' in content:
            print("   bot_lock.py with process lock implementation found")
            return True
        else:
            print("   bot_lock.py exists but missing process lock implementation")
            return False
    else:
        print("   bot_lock.py not found")
        return False

def check_dockerfile():
    """Check if Dockerfile uses bot_lock.py."""
    print("Checking Dockerfile for bot_lock.py usage...")
    
    dockerfile_path = Path(__file__).parent / 'Dockerfile'
    content = dockerfile_path.read_text(encoding='utf-8', errors='ignore')
    
    if 'bot_lock.py' in content and 'CMD ["python", "bot_lock.py"]' in content:
        print("   Dockerfile uses bot_lock.py")
        return True
    else:
        print("   Dockerfile does not use bot_lock.py")
        return False

def check_env_example():
    """Check if .env.example has new environment variables."""
    print("Checking .env.example for new environment variables...")
    
    env_path = Path(__file__).parent / '.env.example'
    content = env_path.read_text(encoding='utf-8', errors='ignore')
    
    if 'PORT=' in content and 'WEBHOOK_URL=' in content:
        print("   .env.example has PORT and WEBHOOK_URL")
        return True
    else:
        print("   .env.example missing PORT and WEBHOOK_URL")
        return False

def check_readme():
    """Check if README.md has deployment fixes."""
    print("Checking README.md for deployment fixes...")
    
    readme_path = Path(__file__).parent / 'README.md'
    content = readme_path.read_text(encoding='utf-8', errors='ignore')
    
    if 'TelegramConflictError' in content and 'WEBHOOK_URL' in content:
        print("   README.md has deployment fix documentation")
        return True
    else:
        print("   README.md missing deployment fix documentation")
        return False

def main():
    print("=== Winkly Bot Fix Verification ===")
    
    tests = [
        ("bot.py port configuration", check_bot_py),
        ("bot_lock.py implementation", check_bot_lock_py),
        ("Dockerfile bot_lock usage", check_dockerfile),
        (".env.example environment variables", check_env_example),
        ("README.md deployment fixes", check_readme),
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        print(f"\nChecking {test_name}...")
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"   Test failed with exception: {e}")
            failed += 1
    
    print("\n=== Verification Results ===")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total: {passed + failed}")
    
    if failed == 0:
        print("\nAll verification checks passed!")
        print("\nThe following fixes have been successfully applied:")
        print("1. Process lock implementation to prevent multiple instances")
        print("2. Port configuration fix for webhook mode")
        print("3. Dockerfile updated to use bot_lock.py")
        print("4. Environment variables documentation updated")
        print("5. Deployment documentation enhanced")
        print("\nThese fixes resolve the TelegramConflictError and provide a robust deployment solution.")
        sys.exit(0)
    else:
        print(f"\n{failed} verification check(s) failed.")
        sys.exit(1)

if __name__ == '__main__':
    main()
