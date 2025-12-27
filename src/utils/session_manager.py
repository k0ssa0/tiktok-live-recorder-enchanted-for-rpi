"""
Session Manager for TikTok Live Recorder

Manages session persistence so users can reconnect to view the terminal output
of a running recording session after SSH disconnection.
"""

import json
import os
import subprocess
import signal
import sys
import select
import fcntl
import time
from datetime import datetime
from threading import Thread, Event
from typing import Optional, Dict, Any

SESSION_FILE = "/tmp/tiktok_recorder_session.json"
COMMAND_FILE = "/tmp/tiktok_recorder_command"
SESSION_UPDATE_INTERVAL = 5  # seconds


class SessionManager:
    """Manages session state persistence for reconnection support."""
    
    def __init__(self):
        self.session_file = SESSION_FILE
        self.command_file = COMMAND_FILE
        self.update_thread: Optional[Thread] = None
        self.stop_updates = Event()
        self.session_data: Dict[str, Any] = {}
        self.pid = os.getpid()
        self.log_file: Optional[str] = None
    
    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is still running."""
        try:
            os.kill(pid, 0)  # Signal 0 doesn't kill, just checks
            return True
        except OSError:
            return False
    
    def check_existing_session(self) -> Optional[Dict[str, Any]]:
        """Check if there's an existing running session."""
        if not os.path.exists(self.session_file):
            return None
        
        try:
            with open(self.session_file, 'r') as f:
                data = json.load(f)
            
            # Check if the PID is still running
            if 'pid' in data and self._is_process_running(data['pid']):
                return data
            else:
                # Stale session file, remove it
                os.remove(self.session_file)
                return None
        except (json.JSONDecodeError, IOError):
            return None
    
    def send_command(self, command: str):
        """Send a command to the running session."""
        try:
            with open(self.command_file, 'w') as f:
                f.write(command)
        except IOError:
            pass
    
    def read_command(self) -> Optional[str]:
        """Read and clear any pending command. Called by the running session."""
        if not os.path.exists(self.command_file):
            return None
        try:
            with open(self.command_file, 'r') as f:
                command = f.read().strip()
            os.remove(self.command_file)  # Clear the command
            return command if command else None
        except IOError:
            return None
    
    def prompt_reconnect(self) -> str:
        """
        Prompt user if they want to reconnect to existing session.
        Returns: 'y' to view session, 'n' to kill and start new, 'q' to quit
        """
        session = self.check_existing_session()
        if not session:
            return 'new'
        
        print("\n" + "=" * 55)
        print("  EXISTING SESSION DETECTED")
        print("=" * 55)
        print(f"  User: @{session.get('user', 'N/A')}")
        print(f"  State: {session.get('state', 'N/A')}")
        print(f"  Started: {session.get('started_at', 'N/A')}")
        print(f"  PID: {session.get('pid', 'N/A')}")
        
        if session.get('log_file') and os.path.exists(session['log_file']):
            print(f"  Log: {session.get('log_file')}")
        
        print("=" * 55)
        print()
        
        while True:
            try:
                choice = input("Reconnect to view session output? [y/n/q]: ").strip().lower()
                if choice in ('y', 'yes'):
                    return 'y'
                elif choice in ('n', 'no'):
                    return 'n'
                elif choice in ('q', 'quit'):
                    return 'q'
                print("Please enter 'y' (reconnect), 'n' (kill & start new), or 'q' (quit)")
            except (EOFError, KeyboardInterrupt):
                print()
                return 'q'
    
    def view_session_output(self) -> bool:
        """
        Attach to existing session by tailing its log file.
        Allows sending commands to the running session.
        Returns True if successfully attached, False otherwise.
        """
        session = self.check_existing_session()
        if not session:
            print("Session no longer running.")
            return False
        
        log_file = session.get('log_file')
        if not log_file or not os.path.exists(log_file):
            print(f"\n[!] Log file not found. Session is running (PID: {session.get('pid')})")
            print("[!] Run the original script with --verbose flag to enable log reconnection.")
            print(f"\n[*] You can manually check the process: ps -p {session.get('pid')}")
            print(f"[*] Or kill it with: kill {session.get('pid')}")
            return False
        
        print(f"\n[*] Reconnecting to session output...")
        print(f"[*] Tailing: {log_file}")
        print("-" * 55)
        print("  Commands: [f]=force recheck  [Ctrl+C]=detach")
        print("-" * 55 + "\n")
        
        try:
            # Use tail -f to follow the log file
            tail_process = subprocess.Popen(
                ['tail', '-f', '-n', '50', log_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Make stdout non-blocking for tail
            import fcntl
            fd_tail = tail_process.stdout.fileno()
            fl = fcntl.fcntl(fd_tail, fcntl.F_GETFL)
            fcntl.fcntl(fd_tail, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            
            print(f"[*] Reconnecting to session output...")
            print(f"[*] Tailing: {log_file}")
            print("-" * 55)
            print("  Commands: [f]=force recheck  [Enter]=status  [Ctrl+C]=detach")
            print("-" * 55 + "\n")
            
            # Monitor both the tail process and user input
            while True:
                # Check if session is still running
                if not self._is_process_running(session.get('pid', 0)):
                    print("\n" + "-" * 55)
                    print("[*] Session ended.")
                    tail_process.terminate()
                    break
                
                # Wait for input from either stdin or tail output
                # select allows us to wait efficiently without busy looping
                rlist, _, _ = select.select([sys.stdin, tail_process.stdout], [], [], 0.1)
                
                for source in rlist:
                    if source is sys.stdin:
                        # Handle User Input
                        user_input = sys.stdin.readline().strip().lower()
                        if user_input == 'f':
                            print("\n[?] Force recheck now? (y/n): ", end='', flush=True)
                            # Wait for confirmation with timeout
                            if select.select([sys.stdin], [], [], 10.0)[0]:
                                confirm = sys.stdin.readline().strip().lower()
                                if confirm in ('y', 'yes'):
                                    self.send_command('force_recheck')
                                    print("[*] Force recheck command sent!\n")
                                else:
                                    print("[*] Cancelled.\n")
                            else:
                                print("\n[*] Timeout.\n")
                        elif user_input == '': # Enter or empty input
                             self.send_command('status')
                    
                    elif source is tail_process.stdout:
                        # Handle Log Output
                        try:
                            # Read all available lines
                            while True:
                                line = tail_process.stdout.readline()
                                if not line:
                                    break
                                print(line, end='')
                        except IOError:
                            pass  # No more data available right now
                    
        except KeyboardInterrupt:
            print("\n" + "-" * 55)
            print("[*] Detached from session. (Session still running in background)")
            tail_process.terminate()
        except FileNotFoundError:
            print("[!] Error: 'tail' command not found.")
            return False
        
        return True
    
    def kill_existing_session(self) -> bool:
        """Kill the existing session."""
        session = self.check_existing_session()
        if not session:
            return True
        
        pid = session.get('pid')
        if pid:
            try:
                print(f"[*] Stopping existing session (PID: {pid})...")
                os.kill(pid, signal.SIGTERM)
                
                # Wait for process to terminate
                for _ in range(10):
                    time.sleep(0.5)
                    if not self._is_process_running(pid):
                        print("[*] Previous session stopped.")
                        break
                else:
                    # Force kill if still running
                    print(f"[*] Force stopping PID {pid}...")
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(1)
                
                # Clean up session file
                if os.path.exists(self.session_file):
                    os.remove(self.session_file)
                return True
            except OSError as e:
                print(f"[!] Error stopping process: {e}")
                return False
        return True
    
    def start_session(self, user: str, log_file: Optional[str] = None):
        """Start a new session and begin updating the session file."""
        self.log_file = log_file
        self.session_data = {
            'pid': self.pid,
            'user': user,
            'state': 'starting',
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'log_file': log_file,
        }
        self._write_session()
        
        # Start background update thread
        self.stop_updates.clear()
        self.update_thread = Thread(target=self._update_loop, daemon=True)
        self.update_thread.start()
    
    def _update_loop(self):
        """Background loop to periodically update session file."""
        while not self.stop_updates.is_set():
            self._write_session()
            time.sleep(SESSION_UPDATE_INTERVAL)
    
    def _write_session(self):
        """Write current session data to file."""
        self.session_data['last_update'] = datetime.now().isoformat()
        try:
            with open(self.session_file, 'w') as f:
                json.dump(self.session_data, f, indent=2)
        except IOError:
            pass  # Silently ignore write errors
    
    def update(self, **kwargs):
        """Update session data with new values."""
        self.session_data.update(kwargs)
    
    def end_session(self):
        """End the session and clean up."""
        self.stop_updates.set()
        if self.update_thread and self.update_thread.is_alive():
            self.update_thread.join(timeout=2)
        
        # Remove session file
        try:
            if os.path.exists(self.session_file):
                os.remove(self.session_file)
        except IOError:
            pass


# Global session manager instance
session_manager = SessionManager()
