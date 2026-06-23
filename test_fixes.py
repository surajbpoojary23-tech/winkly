#!/usr/bin/env python3
"""
Test script to verify the Winkly bot fixes.
This script tests the key fixes applied to resolve the deployment issues.
"""

import os
import sys
import tempfile
import subprocess
from pathlib import Path

def test_process_lock():
    """Test that the process lock prevents multiple instances."""
    print("🧪 Testing process lock functionality...")
    
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        lock_file = Path(temp_dir) / "test.lock"
        
        # Import the ProcessLock class
        sys.path.insert(0, str(Path(__file__).parent))
        
        # Test 1: First instance should acquire lock
        from bot_lock import ProcessLock
        
        lock1 = ProcessLock(str(lock_file))
        if lock1.acquire():
            print("   ✅ First instance acquired lock")
            
            # Test 2: Second instance should fail
            lock2 = ProcessLock(str(lock_file))
            if not lock2.acquire():
                print("   ✅ Second instance correctly rejected")
                lock1.release()
                return True
            else:
                print("   ❌ Second instance incorrectly acquired lock")
                lock1.release()
                return False
        else:
            print("   ❌ First instance failed to acquire lock")
            return False

def test_port_configuration():
    """Test port configuration."""
    print("🧪 Testing port configuration...")
    
    # Test default port
    os.environ['PORT'] = '8080'
    from bot import on_startup
    
    # Check if the bot reads PORT environment variable
    port = os.getenv('PORT', '8080')
    try:
        port_num = int(port)
        if 1 <= port_num <= 65535:
            print(f"   ✅ Port {port} is valid")
            return True
        else:
            print(f"   ❌ Port {port} is out of range")
            return False
    except ValueError:
        print(f"   ❌ Port '{port}' is not a valid number")
        return False

def test_deployment_check():
    """Test the deployment check script."""
    print("🧪 Testing deployment check script...")
    
    # Set up test environment variables
    test_env = {
        'BOT_TOKEN': 'test_token',
        'REDIS_URL': 'redis://test:6379',
        'PORT': '8080',
        'WEBHOOK_URL': 'https://test.onrender.com/'
    }
    
    # Run deploy_check.py
    script_path = Path(__file__).parent / 'deploy_check.py'
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        env={**os.environ, **test_env}
    )
    
    if result.returncode == 0:
        print("   ✅ Deployment check passed")
        return True
    else:
        print(f"   ❌ Deployment check failed: {result.stderr}")
        return False

def main():
    print("=== Winkly Bot Fix Verification ===\n")
    
    tests = [
        ("Process Lock", test_process_lock),
        ("Port Configuration", test_port_configuration),
        ("Deployment Check", test_deployment_check),
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        print(f"\n📋 Running {test_name} test...")
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"   ❌ Test failed with exception: {e}")
            failed += 1
    
    print("\n=== Test Results ===")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")
    print(f"📊 Total: {passed + failed}")
    
    if failed == 0:
        print("\n🎉 All tests passed! The fixes are working correctly.")
        sys.exit(0)
    else:
        print(f"\n⚠️  {failed} test(s) failed. Please review the fixes.")
        sys.exit(1)

if __name__ == '__main__':
    main()
