#!/usr/bin/env python3
"""
Process lock to prevent multiple bot instances from running simultaneously.
This prevents TelegramConflictError when multiple instances try to fetch updates.
"""

import os
import sys
import fcntl
import atexit

class ProcessLock:
    def __init__(self, lock_file_path):
        self.lock_file_path = lock_file_path
        self.lock_file = None
        self.is_locked = False
    
    def acquire(self):
        """Acquire an exclusive lock on the lock file."""
        try:
            self.lock_file = open(self.lock_file_path, 'w')
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.is_locked = True
            
            # Write our process ID to the lock file for debugging
            self.lock_file.write(str(os.getpid()))
            self.lock_file.flush()
            
            # Register cleanup function
            atexit.register(self.release)
            
            return True
        except (IOError, OSError):
            # Lock is already held by another process
            if self.lock_file:
                self.lock_file.close()
            print("❌ Another bot instance is already running!")
            print("   This prevents TelegramConflictError.")
            print("   Please stop the other instance and try again.")
            return False
    
    def release(self):
        """Release the lock."""
        if self.is_locked and self.lock_file:
            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
                self.lock_file.close()
            except:
                pass
            self.is_locked = False
            self.lock_file = None

def main():
    # Create lock file in the bot directory
    lock_file_path = os.path.join(os.path.dirname(__file__), '.bot_instance.lock')
    lock = ProcessLock(lock_file_path)
    
    if not lock.acquire():
        sys.exit(1)
    
    print(f"✅ Bot instance locked (PID: {os.getpid()})")
    
    try:
        # Import and run the bot
        from bot import on_startup, dp, bot
        import asyncio
        
        # Run everything in a single event loop
        async def startup_tasks():
            # Force reset any stale webhook or long-poll session
            await bot.delete_webhook(drop_pending_updates=True)
            print("✅ Cleared any existing Telegram webhook/session")
            
            # Now start the bot
            await on_startup(dp)
        
        # Run the bot
        asyncio.run(startup_tasks())
        
    except KeyboardInterrupt:
        print("\n⚠️  Bot stopped by user")
    except Exception as e:
        print(f"❌ Bot error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        lock.release()
        print("✅ Bot instance unlocked")

if __name__ == '__main__':
    main()
