import os
import random
import select
import sys
import termios
import time
from datetime import datetime, timedelta
from http.client import HTTPException
from threading import Thread, Event
from typing import Optional

from requests import RequestException

from core.tiktok_api import TikTokAPI
from utils.logger_manager import logger
from utils.video_management import VideoManagement
from upload.telegram import Telegram
from utils.custom_exceptions import LiveNotFound, UserLiveError, TikTokRecorderError, SigningAPIError
from utils.enums import Mode, Error, TimeOut, TikTokError
from utils.session_manager import session_manager


# Recording configuration constants
class RecordingConfig:
    BUFFER_SIZE = 512 * 1024  # 512 KB buffer
    ALIVE_CHECK_INTERVAL = 30  # seconds
    PROGRESS_LOG_INTERVAL = 60  # seconds
    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_DELAY = 5  # seconds
    
    # Jitter settings to avoid WAF detection
    JITTER_MIN = 0.7  # Minimum multiplier (70% of interval)
    JITTER_MAX = 1.3  # Maximum multiplier (130% of interval)
    API_CALL_DELAY_MIN = 1.0  # Minimum delay between API calls (seconds)
    API_CALL_DELAY_MAX = 3.0  # Maximum delay between API calls (seconds)


# Raspberry Pi built-in LED controller
class RaspberryPiLED:
    """Control Raspberry Pi's built-in LEDs (green ACT and red PWR)."""
    
    # Green LED paths (ACT/activity LED)
    GREEN_LED_PATHS = [
        "/sys/class/leds/ACT",
        "/sys/class/leds/led0", 
        "/sys/class/leds/mmc0",
    ]
    
    # Red LED paths (PWR/power LED)
    RED_LED_PATHS = [
        "/sys/class/leds/PWR",
        "/sys/class/leds/led1",
    ]
    
    def __init__(self):
        self.green_led_path = None
        self.red_led_path = None
        self.green_original_trigger = None
        self.red_original_trigger = None
        self.blink_thread = None
        self.stop_blink = Event()
        self.green_controlled = False
        self.red_controlled = False
        self._find_leds()
    
    def _find_leds(self):
        """Find the available LED paths."""
        for path in self.GREEN_LED_PATHS:
            if os.path.exists(path):
                self.green_led_path = path
                logger.debug(f"Found Raspberry Pi GREEN LED at: {path}")
                break
        
        for path in self.RED_LED_PATHS:
            if os.path.exists(path):
                self.red_led_path = path
                logger.debug(f"Found Raspberry Pi RED LED at: {path}")
                break
        
        if not self.green_led_path and not self.red_led_path:
            logger.debug("Raspberry Pi LEDs not found (not running on Pi?)")
    
    def _write_to_led(self, led_path: str, filename: str, value: str) -> bool:
        """Write value to LED control file."""
        if not led_path:
            return False
        try:
            with open(f"{led_path}/{filename}", "w") as f:
                f.write(value)
            return True
        except (PermissionError, IOError) as e:
            logger.debug(f"Cannot control LED: {e}")
            return False
    
    def _read_from_led(self, led_path: str, filename: str) -> Optional[str]:
        """Read value from LED control file."""
        if not led_path:
            return None
        try:
            with open(f"{led_path}/{filename}", "r") as f:
                content = f.read().strip()
                # Extract current trigger (marked with [brackets])
                if filename == "trigger":
                    import re
                    match = re.search(r'\[(\w+)\]', content)
                    return match.group(1) if match else content.split()[0]
                return content
        except (IOError, IndexError):
            return None
    
    def _take_green_control(self):
        """Take manual control of the green LED."""
        if self.green_controlled:
            return True
        if not self.green_led_path:
            return False
        
        self.green_original_trigger = self._read_from_led(self.green_led_path, "trigger")
        if self._write_to_led(self.green_led_path, "trigger", "none"):
            self.green_controlled = True
            logger.debug(f"Green LED control acquired (original trigger: {self.green_original_trigger})")
            return True
        return False
    
    def _take_red_control(self):
        """Take manual control of the red LED."""
        if self.red_controlled:
            return True
        if not self.red_led_path:
            return False
        
        self.red_original_trigger = self._read_from_led(self.red_led_path, "trigger")
        if self._write_to_led(self.red_led_path, "trigger", "none"):
            self.red_controlled = True
            logger.debug(f"Red LED control acquired (original trigger: {self.red_original_trigger})")
            return True
        return False
    
    def _release_green_control(self):
        """Release control of green LED."""
        if not self.green_controlled:
            return
        if self.green_original_trigger and self.green_led_path:
            self._write_to_led(self.green_led_path, "trigger", self.green_original_trigger)
            logger.debug(f"Green LED restored to trigger: {self.green_original_trigger}")
        self.green_controlled = False
    
    def _release_red_control(self):
        """Release control of red LED."""
        if not self.red_controlled:
            return
        if self.red_original_trigger and self.red_led_path:
            self._write_to_led(self.red_led_path, "trigger", self.red_original_trigger)
            logger.debug(f"Red LED restored to trigger: {self.red_original_trigger}")
        self.red_controlled = False
    
    def turn_on(self):
        """Turn green LED on (static, no blinking)."""
        if not self.green_led_path:
            return
        if not self._take_green_control():
            return
        self._write_to_led(self.green_led_path, "brightness", "1")
        logger.debug("Green LED turned ON")
    
    def turn_off(self):
        """Turn green LED off."""
        if not self.green_led_path:
            return
        if not self._take_green_control():
            return
        self._write_to_led(self.green_led_path, "brightness", "0")
        logger.debug("Green LED turned OFF")
    
    def error_on(self):
        """Turn red LED on to indicate error state."""
        # First turn off green LED
        self.stop_blinking()
        self.turn_off()
        
        if not self.red_led_path:
            return
        if not self._take_red_control():
            return
        self._write_to_led(self.red_led_path, "brightness", "1")
        logger.debug("Red LED turned ON (error state)")
    
    def error_off(self):
        """Turn red LED off (clear error state)."""
        if not self.red_led_path:
            return
        if not self._take_red_control():
            return
        self._write_to_led(self.red_led_path, "brightness", "0")
        logger.debug("Red LED turned OFF")
        self._release_red_control()
    
    def start_blinking(self, interval: float = 0.5):
        """Start alternating blink of green and red LEDs in a background thread."""
        if not self.green_led_path and not self.red_led_path:
            return
        
        self._take_green_control()
        self._take_red_control()
        
        logger.debug("LEDs alternating blink started")
        
        self.stop_blink.clear()
        
        def blink_loop():
            green_on = True
            while not self.stop_blink.is_set():
                # Alternate: when green is ON, red is OFF and vice versa
                if self.green_led_path:
                    self._write_to_led(self.green_led_path, "brightness", "1" if green_on else "0")
                if self.red_led_path:
                    self._write_to_led(self.red_led_path, "brightness", "0" if green_on else "1")
                green_on = not green_on
                time.sleep(interval)
        
        self.blink_thread = Thread(target=blink_loop, daemon=True)
        self.blink_thread.start()
    
    def stop_blinking(self):
        """Stop blinking and restore original LED behavior."""
        if not self.green_led_path and not self.red_led_path:
            return
        
        self.stop_blink.set()
        
        if self.blink_thread and self.blink_thread.is_alive():
            self.blink_thread.join(timeout=2)
        
        self.blink_thread = None
        
        # Turn off both LEDs and release control
        if self.green_led_path:
            self._write_to_led(self.green_led_path, "brightness", "0")
        if self.red_led_path:
            self._write_to_led(self.red_led_path, "brightness", "0")
        
        self._release_green_control()
        self._release_red_control()


# Global LED controller instance
pi_led = RaspberryPiLED()

# Global status tracking for interactive monitoring
class StatusTracker:
    def __init__(self):
        self.next_check_time: Optional[datetime] = None
        self.last_check_time: Optional[datetime] = None
        self.check_count: int = 0
        self.current_state: str = "initializing"
        self.user: str = ""
        self.room_id: str = ""
        self.force_recheck = Event()  # Event to signal force recheck
        # Recording tracking
        self.recording_file: str = ""
        self.recording_start_time: Optional[datetime] = None
        self.recording_bytes: int = 0
    
    def start_recording_tracking(self, file_path: str):
        """Start tracking a recording session."""
        self.recording_file = file_path
        self.recording_start_time = datetime.now()
        self.recording_bytes = 0
    
    def update_recording_bytes(self, bytes_written: int):
        """Update the bytes written during recording."""
        self.recording_bytes = bytes_written
    
    def stop_recording_tracking(self):
        """Stop tracking recording session."""
        self.recording_file = ""
        self.recording_start_time = None
        self.recording_bytes = 0
    
    def _format_duration(self, seconds: float) -> str:
        """Format seconds into HH:MM:SS."""
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"
    
    def _format_size(self, bytes_val: int) -> str:
        """Format bytes into human readable size."""
        if bytes_val >= 1024 * 1024 * 1024:
            return f"{bytes_val / (1024*1024*1024):.2f} GB"
        elif bytes_val >= 1024 * 1024:
            return f"{bytes_val / (1024*1024):.2f} MB"
        elif bytes_val >= 1024:
            return f"{bytes_val / 1024:.2f} KB"
        else:
            return f"{bytes_val} B"
    
    def get_status(self) -> str:
        now = datetime.now()
        lines = [
            "\n" + "-" * 50,
            f"  STATUS @ {now.strftime('%H:%M:%S')} (script still running)",
            "-" * 50,
            f"  State: {self.current_state}",
            f"  User:  @{self.user}",
            f"  Room:  {self.room_id or 'N/A'}",
            f"  Checks: {self.check_count}",
        ]
        
        # Show recording info if currently recording
        if self.current_state == "recording" and self.recording_start_time:
            lines.append("-" * 50)
            lines.append("  📹 RECORDING IN PROGRESS")
            
            # Duration
            elapsed = (now - self.recording_start_time).total_seconds()
            lines.append(f"  Duration: {self._format_duration(elapsed)}")
            
            # File size
            lines.append(f"  Size: {self._format_size(self.recording_bytes)}")
            
            # Bitrate (if we have enough data)
            if elapsed > 5 and self.recording_bytes > 0:
                bitrate_kbps = (self.recording_bytes * 8) / (elapsed * 1000)
                lines.append(f"  Bitrate: {bitrate_kbps:.0f} kbps")
            
            # File path
            if self.recording_file:
                lines.append(f"  File: {os.path.basename(self.recording_file)}")
            lines.append("-" * 50)
        
        if self.last_check_time:
            elapsed = now - self.last_check_time
            lines.append(f"  Last check: {self.last_check_time.strftime('%H:%M:%S')} ({int(elapsed.total_seconds())}s ago)")
        
        # Only show next check info when not recording (it's confusing during recording)
        if self.current_state != "recording" and self.next_check_time:
            remaining = self.next_check_time - now
            if remaining.total_seconds() > 0:
                mins, secs = divmod(int(remaining.total_seconds()), 60)
                lines.append(f"  Next check in: {mins}m {secs}s (at {self.next_check_time.strftime('%H:%M:%S')})")
            else:
                lines.append("  Next check: NOW (processing...)")
        
        lines.append("-" * 50)
        lines.append("  [Enter] = status | [f] = force | [c] = cookies | [r] = room cache | [q] = quit")
        lines.append("-" * 50 + "\n")
        return "\n".join(lines)


# Global status tracker instance
status_tracker = StatusTracker()


def _getch_with_timeout(fd, timeout: float) -> str:
    """Read a single character with timeout. Returns empty string on timeout."""
    if select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return ''


def start_input_listener():
    """Start a background thread that listens for keyboard input.
    
    Uses termios cbreak mode if available, falls back to line-based input.
    """
    from utils.utils import save_cookies
    from core.tiktok_api import TikTokAPI
    
    def listener_cbreak():
        """Listener using cbreak mode (single keypress)."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        
        try:
            # Set terminal to cbreak mode
            import tty
            tty.setcbreak(fd)
            logger.debug("Terminal set to cbreak mode")
            
            while True:
                try:
                    char = _getch_with_timeout(fd, 1.0)
                    if not char:
                        continue
                    
                    logger.debug(f"Key pressed: {repr(char)}")
                    key = char.lower()
                    
                    if key == 'f':
                        print("\n[?] Force recheck now? (y/n): ", end='', flush=True)
                        confirm = _getch_with_timeout(fd, 10.0).lower()
                        print(confirm if confirm else "")
                        if confirm == 'y':
                            print("[*] Force recheck requested...\n")
                            status_tracker.force_recheck.set()
                        else:
                            print("[*] Cancelled.\n")
                    
                    elif key == 'c':
                        print("\n[?] Change cookies? (y/n): ", end='', flush=True)
                        confirm = _getch_with_timeout(fd, 10.0).lower()
                        print(confirm if confirm else "")
                        if confirm == 'y':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                            print("[*] Paste new sessionid_ss cookie (then press Enter):")
                            print("[>] ", end='', flush=True)
                            if select.select([sys.stdin], [], [], 60.0)[0]:
                                new_cookie = sys.stdin.readline().strip()
                                if new_cookie and new_cookie.lower() != 'cancel':
                                    try:
                                        save_cookies(new_cookie)
                                        print("[*] Cookie updated!")
                                        print(f"[*] New: {new_cookie[:10]}...{new_cookie[-5:]}\n")
                                        logger.info("Cookies updated via interactive input")
                                    except Exception as e:
                                        print(f"[!] Error: {e}\n")
                                else:
                                    print("[*] Cancelled.\n")
                            else:
                                print("\n[*] Timeout.\n")
                            tty.setcbreak(fd)
                        else:
                            print("[*] Cancelled.\n")
                    
                    elif key == 'r':
                        user = status_tracker.user
                        cached_id = TikTokAPI.get_cached_room_id(user)
                        print("\n" + "-" * 50)
                        print("  ROOM ID CACHE")
                        print("-" * 50)
                        print(f"  User: @{user}")
                        print(f"  Cached: {cached_id or 'None'}")
                        print(f"  Current: {status_tracker.room_id or 'None'}")
                        print("-" * 50)
                        print("  [v]=view | [c]=clear | [s]=set | [Enter]=back")
                        print("-" * 50 + "\n[>] ", end='', flush=True)
                        
                        sub = _getch_with_timeout(fd, 30.0).lower()
                        print(sub if sub else "")
                        if sub == 'v':
                            from core.tiktok_api import ROOM_ID_CACHE_FILE
                            if ROOM_ID_CACHE_FILE.exists():
                                print(f"\n[*] Cache file ({ROOM_ID_CACHE_FILE}):")
                                with open(ROOM_ID_CACHE_FILE, 'r') as f:
                                    print(f.read())
                            else:
                                print("\n[*] No cache file exists.\n")
                        elif sub == 'c':
                            print("[?] Clear: [u]=user [a]=all [other]=cancel: ", end='', flush=True)
                            opt = _getch_with_timeout(fd, 10.0).lower()
                            print(opt if opt else "")
                            if opt == 'a':
                                TikTokAPI.clear_cached_room_id()  # No argument clears all
                                print("[*] Cleared all cached room IDs.\n")
                            elif opt == 'u':
                                TikTokAPI.clear_cached_room_id(user)
                                print(f"[*] Cleared cache for @{user}.\n")
                            else:
                                print("[*] Cancelled.\n")
                        elif sub == 's':
                            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                            print("[*] Enter room_id: ", end='', flush=True)
                            if select.select([sys.stdin], [], [], 30.0)[0]:
                                manual_id = sys.stdin.readline().strip()
                                if manual_id.isdigit():
                                    TikTokAPI.cache_room_id(user, manual_id)
                                    print(f"[*] Cached room_id {manual_id} for @{user}.\n")
                                else:
                                    print("[!] Invalid (must be numeric).\n")
                            else:
                                print("\n[*] Timeout.\n")
                            tty.setcbreak(fd)
                    
                    elif key == 'q':
                        print("\n[?] Quit application? (y/n): ", end='', flush=True)
                        confirm = _getch_with_timeout(fd, 10.0).lower()
                        print(confirm if confirm else "")
                        if confirm == 'y':
                            print("[*] Quitting...\n")
                            import os
                            os._exit(0)
                        else:
                            print("[*] Cancelled.\n")
                    
                    elif key in ('\r', '\n', ' '):
                        print(status_tracker.get_status())
                    
                except Exception as e:
                    logger.debug(f"Input error: {e}")
                    time.sleep(1)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
    
    def listener_readline():
        """Fallback listener using readline (requires Enter key)."""
        logger.debug("Using readline fallback mode (press Enter after command)")
        print("[*] Note: Type command then press Enter (f=force, c=cookies, r=room, Enter=status)")
        
        while True:
            try:
                if select.select([sys.stdin], [], [], 1.0)[0]:
                    line = sys.stdin.readline().strip().lower()
                    
                    if line == 'f':
                        print("[?] Force recheck now? (y/n): ", end='', flush=True)
                        if select.select([sys.stdin], [], [], 10.0)[0]:
                            confirm = sys.stdin.readline().strip().lower()
                            if confirm in ('y', 'yes'):
                                print("[*] Force recheck requested...\n")
                                status_tracker.force_recheck.set()
                            else:
                                print("[*] Cancelled.\n")
                    
                    elif line == 'c':
                        print("[?] Change cookies? (y/n): ", end='', flush=True)
                        if select.select([sys.stdin], [], [], 10.0)[0]:
                            confirm = sys.stdin.readline().strip().lower()
                            if confirm in ('y', 'yes'):
                                print("[*] Paste new sessionid_ss cookie:")
                                print("[>] ", end='', flush=True)
                                if select.select([sys.stdin], [], [], 60.0)[0]:
                                    new_cookie = sys.stdin.readline().strip()
                                    if new_cookie and new_cookie.lower() != 'cancel':
                                        try:
                                            save_cookies(new_cookie)
                                            print("[*] Cookie updated!\n")
                                        except Exception as e:
                                            print(f"[!] Error: {e}\n")
                    
                    elif line == 'r':
                        user = status_tracker.user
                        cached_id = TikTokAPI.get_cached_room_id(user)
                        print("\n" + "-" * 50)
                        print(f"  User: @{user}")
                        print(f"  Cached: {cached_id or 'None'}")
                        print(f"  Current: {status_tracker.room_id or 'None'}")
                        print("-" * 50)
                        print("  Type: v=view, c=clear, s=set, Enter=back")
                    
                    elif line == 'q':
                        print("[?] Quit application? (y/n): ", end='', flush=True)
                        if select.select([sys.stdin], [], [], 10.0)[0]:
                            confirm = sys.stdin.readline().strip().lower()
                            if confirm in ('y', 'yes'):
                                print("[*] Quitting...\n")
                                import os
                                os._exit(0)
                            else:
                                print("[*] Cancelled.\n")
                        
                    else:
                        # Any other input shows status
                        print(status_tracker.get_status())
                        
            except Exception as e:
                logger.debug(f"Input error: {e}")
                time.sleep(1)
    
    def listener():
        # Check if stdin is a TTY
        if not sys.stdin.isatty():
            logger.debug("stdin is not a TTY, input listener disabled")
            return
        
        # Try cbreak mode first, fall back to readline
        try:
            fd = sys.stdin.fileno()
            termios.tcgetattr(fd)  # Test if we can get terminal attrs
            listener_cbreak()
        except Exception as e:
            logger.debug(f"cbreak mode failed ({e}), using readline fallback")
            listener_readline()
    
    thread = Thread(target=listener, daemon=True)
    thread.start()
    return thread


def start_remote_command_listener():
    """Start a thread to listen for remote commands via SessionManager."""
    def remote_listener():
        while True:
            command = session_manager.read_command()
            if command:
                if command == 'status':
                    # Log status so it appears in the log file (and thus the viewer)
                    # We use logger.info but with a marker or just as is
                    logger.info(status_tracker.get_status())
                elif command == 'force_recheck':
                    logger.info("Remote force recheck requested")
                    status_tracker.force_recheck.set()
            time.sleep(0.5)
            
    thread = Thread(target=remote_listener, daemon=True)
    thread.start()
    return thread


def jitter_sleep(base_seconds: float, min_mult: float = RecordingConfig.JITTER_MIN, 
                 max_mult: float = RecordingConfig.JITTER_MAX) -> float:
    """
    Sleep for a randomized duration based on the base time.
    Can be interrupted by force_recheck event or remote command.
    
    Args:
        base_seconds: Base sleep time in seconds
        min_mult: Minimum multiplier for jitter
        max_mult: Maximum multiplier for jitter
        
    Returns:
        Actual sleep duration
    """
    jittered = base_seconds * random.uniform(min_mult, max_mult)
    status_tracker.next_check_time = datetime.now() + timedelta(seconds=jittered)
    status_tracker.current_state = "waiting for next check"
    status_tracker.force_recheck.clear()  # Reset the event
    
    logger.debug(f"Sleeping for {jittered:.1f}s (base: {base_seconds}s)")
    logger.info(f"Next check at: {status_tracker.next_check_time.strftime('%H:%M:%S')} (Enter=status, f=force recheck)")
    
    # Sleep in small intervals to allow for force recheck interruption
    sleep_interval = 0.5  # Check every 0.5 seconds for responsiveness
    elapsed = 0.0
    while elapsed < jittered:
        # Check local force recheck event (from direct terminal input or remote thread)
        if status_tracker.force_recheck.is_set():
            logger.info("Force recheck triggered!")
            status_tracker.force_recheck.clear()
            return elapsed  # Return early
        
        time.sleep(min(sleep_interval, jittered - elapsed))
        elapsed += sleep_interval
    
    return jittered


def api_delay():
    """Add a small random delay before API calls to avoid rate limiting."""
    delay = random.uniform(RecordingConfig.API_CALL_DELAY_MIN, RecordingConfig.API_CALL_DELAY_MAX)
    logger.debug(f"API delay: {delay:.2f}s")
    time.sleep(delay)


class TikTokRecorder:
    def __init__(
        self,
        url,
        user,
        room_id,
        mode,
        automatic_interval,
        cookies,
        proxy,
        output,
        duration,
        use_telegram,
    ):
        # Setup TikTok API client
        self.tiktok = TikTokAPI(proxy=proxy, cookies=cookies)

        # TikTok Data
        self.url = url
        self.user = user
        self.room_id = room_id

        # Tool Settings
        self.mode = mode
        self.automatic_interval = automatic_interval
        self.duration = duration
        self.output = output

        # Upload Settings
        self.use_telegram = use_telegram

        # Debug logging
        logger.debug(f"TikTokRecorder initialized with: user={user}, room_id={room_id}, mode={mode}")
        logger.debug(f"Settings: interval={automatic_interval}, duration={duration}, output={output}")

        # Check if the user's country is blacklisted
        self.check_country_blacklisted()

        # Retrieve sec_uid if the mode is FOLLOWERS
        if self.mode == Mode.FOLLOWERS:
            self.sec_uid = self.tiktok.get_sec_uid()
            if self.sec_uid is None:
                raise TikTokRecorderError("Failed to retrieve sec_uid.")

            logger.info("Followers mode activated\n")
        else:
            # Get live information based on the provided user data
            if self.url:
                self.user, self.room_id = self.tiktok.get_room_and_user_from_url(
                    self.url
                )

            if not self.user:
                self.user = self.tiktok.get_user_from_room_id(self.room_id)

            # For AUTOMATIC mode, don't fetch room_id during init - let the loop handle it
            # This allows the automatic_mode loop's error handling to manage API failures
            if not self.room_id and self.mode != Mode.AUTOMATIC:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)

            logger.info(f"USERNAME: {self.user}" + ("\n" if not self.room_id else ""))
            if self.room_id:
                logger.info(
                    f"ROOM_ID:  {self.room_id}"
                    + ("\n" if not self.tiktok.is_room_alive(self.room_id) else "")
                )

        # If proxy is provided, set up the HTTP client without the proxy
        if proxy:
            self.tiktok = TikTokAPI(proxy=None, cookies=cookies)

    def run(self):
        """
        runs the program in the selected mode.

        If the mode is MANUAL, it checks if the user is currently live and
        if so, starts recording.

        If the mode is AUTOMATIC, it continuously checks if the user is live
        and if not, waits for the specified timeout before rechecking.
        If the user is live, it starts recording.

        if the mode is FOLLOWERS, it continuously checks the followers of
        the authenticated user. If any follower is live, it starts recording
        their live stream in a separate process.
        """
        if self.mode == Mode.MANUAL:
            self.manual_mode()

        elif self.mode == Mode.AUTOMATIC:
            self.automatic_mode()

        elif self.mode == Mode.FOLLOWERS:
            self.followers_mode()

    def manual_mode(self):
        if not self.room_id:
            raise UserLiveError(f"@{self.user}: Room ID not available")
        if not self.tiktok.is_room_alive(self.room_id):
            raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")

        self.start_recording(self.user, self.room_id)

    def automatic_mode(self):
        # Start input listener for interactive status
        listener_thread = start_input_listener()
        remote_listener_thread = start_remote_command_listener()
        logger.info("Press Enter anytime to see current status\n")
        logger.debug(f"Input listener thread started: {listener_thread.is_alive()}")
        logger.debug(f"Remote listener thread started: {remote_listener_thread.is_alive()}")
        
        # Turn off LED at start (idle state)
        pi_led.turn_off()
        
        status_tracker.user = self.user
        session_manager.update(state="monitoring", user=self.user)
        
        while True:
            try:
                status_tracker.check_count += 1
                status_tracker.current_state = "checking live status"
                status_tracker.last_check_time = datetime.now()
                
                # Turn LED on during check
                pi_led.turn_on()
                
                check_msg = f"[Check #{status_tracker.check_count}] Checking if @{self.user} is live..."
                logger.info(check_msg)
                api_delay()  # Add jitter before API call
                
                status_tracker.current_state = "fetching room ID"
                self.room_id = self.tiktok.get_room_id_from_user(self.user)
                if not self.room_id:
                    raise UserLiveError(f"@{self.user}: Could not retrieve room ID")
                status_tracker.room_id = self.room_id
                logger.debug(f"Room ID retrieved: {self.room_id}")
                
                # Check if user is live before starting
                status_tracker.current_state = "checking if room is alive"
                is_live = self.tiktok.is_room_alive(self.room_id)
                logger.debug(f"Room alive status: {is_live}")
                
                if not is_live:
                    # Keep LED on for 10 seconds total, then turn off
                    time.sleep(10)
                    pi_led.turn_off()
                    raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")
                
                # User is live - LED will switch to blinking in start_recording
                # Record the entire live session (until user goes offline)
                status_tracker.current_state = "recording"
                session_manager.update(state="recording")
                self.start_recording(self.user, self.room_id)
                
                # After recording ends, turn off LED and wait before checking again
                pi_led.turn_off()
                session_manager.update(state="waiting")
                logger.info(f"Recording session ended. Waiting ~{self.automatic_interval} minutes before recheck...\n")
                jitter_sleep(self.automatic_interval * TimeOut.ONE_MINUTE)

            except UserLiveError as ex:
                logger.info(ex)
                pi_led.turn_off()
                session_manager.update(state="waiting")
                wait_time = self.automatic_interval * TimeOut.ONE_MINUTE
                logger.info(f"Waiting ~{self.automatic_interval} minutes before recheck (with jitter)\n")
                jitter_sleep(wait_time)

            except LiveNotFound as ex:
                logger.error(f"Live not found: {ex}")
                pi_led.error_on()  # Red LED on for error
                session_manager.update(state="error")
                wait_time = self.automatic_interval * TimeOut.ONE_MINUTE
                logger.info(f"Waiting ~{self.automatic_interval} minutes before recheck (with jitter)\n")
                jitter_sleep(wait_time)
                pi_led.error_off()  # Clear error after wait

            except SigningAPIError as ex:
                # Signing API is blocked (Cloudflare) - already retried internally, wait briefly
                logger.error(f"Signing API error: {ex}")
                pi_led.error_on()
                session_manager.update(state="error")
                logger.info("Signing API unavailable. Waiting 10 seconds before retry...\n")
                jitter_sleep(10)
                pi_led.error_off()

            except TikTokRecorderError as ex:
                # Handles timeout errors and other API errors
                logger.error(f"API error: {ex}")
                pi_led.error_on()  # Red LED on for error
                session_manager.update(state="error")
                logger.info("Waiting 30 seconds before retry...\n")
                jitter_sleep(30)
                pi_led.error_off()  # Clear error after wait

            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                pi_led.error_on()  # Red LED on for error
                session_manager.update(state="error")
                jitter_sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)
                pi_led.error_off()  # Clear error after wait

            except Exception as ex:
                import traceback
                logger.error(f"Unexpected error: {ex}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                pi_led.error_on()  # Red LED on for error
                jitter_sleep(30)  # Wait with jitter before retrying
                pi_led.error_off()  # Clear error after wait
            
            except BaseException as ex:
                # Catch SystemExit, KeyboardInterrupt, GeneratorExit etc.
                import traceback
                logger.critical(f"Critical exception caught: {type(ex).__name__}: {ex}")
                logger.critical(f"Traceback: {traceback.format_exc()}")
                # Re-raise to allow proper shutdown
                raise

    def followers_mode(self):
        active_recordings = {}  # follower -> Process

        while True:
            try:
                api_delay()  # Add jitter before API call
                followers = self.tiktok.get_followers_list(self.sec_uid)

                for follower in followers:
                    if follower in active_recordings:
                        if not active_recordings[follower].is_alive():
                            logger.info(f"Recording of @{follower} finished.")
                            del active_recordings[follower]
                        else:
                            continue

                    try:
                        api_delay()  # Add jitter before each follower check
                        room_id = self.tiktok.get_room_id_from_user(follower)

                        if not room_id or not self.tiktok.is_room_alive(room_id):
                            # logger.info(f"@{follower} is not live. Skipping...")
                            continue

                        logger.info(f"@{follower} is live. Starting recording...")

                        thread = Thread(
                            target=self.start_recording,
                            args=(follower, room_id),
                            daemon=True,
                        )
                        thread.start()
                        active_recordings[follower] = thread

                        jitter_sleep(2.5)  # Jittered delay between starting recordings

                    except Exception as e:
                        logger.error(f"Error while processing @{follower}: {e}")
                        continue

                print()
                delay = self.automatic_interval * TimeOut.ONE_MINUTE
                logger.info(f"Waiting ~{self.automatic_interval} minutes for the next check (with jitter)...")
                jitter_sleep(delay)

            except UserLiveError as ex:
                logger.info(ex)
                wait_time = self.automatic_interval * TimeOut.ONE_MINUTE
                logger.info(f"Waiting ~{self.automatic_interval} minutes before recheck (with jitter)\n")
                jitter_sleep(wait_time)

            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                jitter_sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

            except Exception as ex:
                logger.error(f"Unexpected error: {ex}\n")
                jitter_sleep(30)  # Wait with jitter before retrying

    def _get_output_path(self, user: str) -> str:
        """Generate the output file path for recording."""
        current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())
        
        # Default to 'videos' folder if no output specified
        output_dir = self.output or "videos"
        
        # Create directory if it doesn't exist
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            logger.debug(f"Created output directory: {output_dir}")
        
        if not output_dir.endswith(os.sep):
            output_dir += os.sep
            
        return f"{output_dir}TK_{user}_{current_date}_flv.mp4"

    def _flush_buffer(self, buffer: bytearray, out_file) -> int:
        """Flush buffer to file and return bytes written."""
        if buffer:
            out_file.write(buffer)
            bytes_written = len(buffer)
            buffer.clear()
            out_file.flush()
            return bytes_written
        return 0

    def _try_get_fresh_url(self, room_id: str, max_retries: int = 2) -> Optional[str]:
        """Attempt to get a fresh stream URL if room is still alive.
        
        Args:
            room_id: The room ID to check
            max_retries: Number of retries on timeout/error (default 2)
        """
        for attempt in range(max_retries):
            try:
                logger.debug(f"Checking if room {room_id} is still alive... (attempt {attempt + 1}/{max_retries})")
                if self.tiktok.is_room_alive(room_id):
                    logger.debug("Room is alive, getting fresh URL...")
                    url = self.tiktok.get_live_url(room_id)
                    if url:
                        logger.debug(f"Got fresh URL: {url[:80]}...")
                    return url
                else:
                    logger.debug("Room is no longer alive")
                    return None
            except Exception as e:
                logger.debug(f"Attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    logger.debug("Retrying in 3 seconds...")
                    time.sleep(3)
                else:
                    logger.debug("All retries exhausted")
        return None

    def start_recording(self, user: str, room_id: str):
        """
        Start recording live stream.
        
        Args:
            user: TikTok username
            room_id: TikTok room ID
        """
        live_url = self.tiktok.get_live_url(room_id)
        if not live_url:
            raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

        output = self._get_output_path(user)

        if self.duration:
            logger.info(f"Started recording for {self.duration} seconds ")
        else:
            logger.info("Started recording...")

        logger.info(f"Output: {output}")
        
        buffer = bytearray()
        logger.info("[PRESS CTRL + C ONCE TO STOP] [ENTER = recording status]")
        
        # Start LED blinking to indicate recording (Raspberry Pi)
        pi_led.start_blinking(interval=0.5)
        
        # Track recording state
        stop_recording = False
        start_time = time.time()
        last_alive_check = time.time()
        last_progress_log = time.time()
        total_bytes_written = 0
        
        # Start recording tracking for status display
        status_tracker.start_recording_tracking(output)
        
        logger.debug(f"Recording config: buffer_size={RecordingConfig.BUFFER_SIZE}, alive_check_interval={RecordingConfig.ALIVE_CHECK_INTERVAL}s")
        logger.debug(f"Live URL: {live_url}")
        
        with open(output, "wb") as out_file:
            while not stop_recording:
                reconnect_attempts = 0
                
                while reconnect_attempts < RecordingConfig.MAX_RECONNECT_ATTEMPTS:
                    try:
                        current_time = time.time()
                        
                        # Periodic room alive check (not every iteration)
                        if current_time - last_alive_check >= RecordingConfig.ALIVE_CHECK_INTERVAL:
                            logger.debug(f"Checking if room {room_id} is still alive...")
                            if not self.tiktok.is_room_alive(room_id):
                                logger.info("User is no longer live. Stopping recording.")
                                stop_recording = True
                                break
                            logger.debug("Room is still alive")
                            last_alive_check = current_time
                        
                        # Periodic progress logging in verbose mode
                        if current_time - last_progress_log >= RecordingConfig.PROGRESS_LOG_INTERVAL:
                            elapsed = current_time - start_time
                            logger.info(f"Recording progress: {elapsed:.0f}s elapsed, {total_bytes_written / (1024*1024):.2f} MB written")
                            last_progress_log = current_time

                        # Download stream chunks
                        chunks_in_batch = 0
                        for chunk in self.tiktok.download_live_stream(live_url):
                            chunks_in_batch += 1
                            buffer.extend(chunk)
                            if len(buffer) >= RecordingConfig.BUFFER_SIZE:
                                total_bytes_written += self._flush_buffer(buffer, out_file)
                                status_tracker.update_recording_bytes(total_bytes_written)

                            # Check duration limit
                            elapsed_time = time.time() - start_time
                            if self.duration and elapsed_time >= self.duration:
                                logger.info(f"Duration limit ({self.duration}s) reached.")
                                stop_recording = True
                                break
                        
                        logger.debug(f"Stream batch ended: received {chunks_in_batch} chunks")

                        # Stream ended - check if user is still live
                        if not stop_recording:
                            logger.info("Stream ended. Checking if user is still live...")
                            self._flush_buffer(buffer, out_file)
                            
                            time.sleep(2)  # Brief pause before reconnect
                            
                            # Check if still live and get fresh URL
                            new_url = self._try_get_fresh_url(room_id)
                            if new_url:
                                live_url = new_url
                                logger.info("Reconnected with fresh stream URL.")
                                reconnect_attempts = 0  # Reset counter on success
                                continue
                            else:
                                logger.info("User is no longer live. Stopping recording.")
                                stop_recording = True
                        break  # Exit reconnect loop

                    except ConnectionError:
                        reconnect_attempts += 1
                        logger.error(f"Connection error. Reconnect attempt {reconnect_attempts}/{RecordingConfig.MAX_RECONNECT_ATTEMPTS}")
                        self._flush_buffer(buffer, out_file)
                        
                        if reconnect_attempts < RecordingConfig.MAX_RECONNECT_ATTEMPTS:
                            time.sleep(RecordingConfig.RECONNECT_DELAY)
                            new_url = self._try_get_fresh_url(room_id)
                            if new_url:
                                live_url = new_url
                                logger.info("Got fresh stream URL after connection error.")
                            elif not self.tiktok.is_room_alive(room_id):
                                logger.info("User is no longer live.")
                                stop_recording = True
                                break
                        else:
                            logger.error("Max reconnection attempts reached.")
                            stop_recording = True

                    except (RequestException, HTTPException) as ex:
                        reconnect_attempts += 1
                        logger.warning(f"Network error: {ex}. Reconnect attempt {reconnect_attempts}/{RecordingConfig.MAX_RECONNECT_ATTEMPTS}")
                        self._flush_buffer(buffer, out_file)
                        
                        if reconnect_attempts < RecordingConfig.MAX_RECONNECT_ATTEMPTS:
                            time.sleep(RecordingConfig.RECONNECT_DELAY)
                            new_url = self._try_get_fresh_url(room_id)
                            if new_url:
                                live_url = new_url
                        else:
                            logger.error("Max reconnection attempts reached.")
                            stop_recording = True

                    except KeyboardInterrupt:
                        logger.info("Recording stopped by user.")
                        stop_recording = True
                        break

                    except Exception as ex:
                        reconnect_attempts += 1
                        logger.error(f"Unexpected error: {ex}")
                        self._flush_buffer(buffer, out_file)
                        
                        if reconnect_attempts < RecordingConfig.MAX_RECONNECT_ATTEMPTS:
                            logger.info(f"Attempting recovery... ({reconnect_attempts}/{RecordingConfig.MAX_RECONNECT_ATTEMPTS})")
                            time.sleep(RecordingConfig.RECONNECT_DELAY)
                        else:
                            logger.error("Max reconnection attempts reached after errors.")
                            stop_recording = True
                
                # If we exhausted reconnect attempts, stop
                if reconnect_attempts >= RecordingConfig.MAX_RECONNECT_ATTEMPTS:
                    stop_recording = True
                    
            # Final buffer flush
            self._flush_buffer(buffer, out_file)

        # Stop LED blinking and recording tracking
        pi_led.stop_blinking()
        status_tracker.stop_recording_tracking()
        
        logger.info(f"Recording finished: {output}\n")
        VideoManagement.convert_flv_to_mp4(output)

        if self.use_telegram:
            Telegram().upload(output.replace("_flv.mp4", ".mp4"))

    def check_country_blacklisted(self):
        is_blacklisted = self.tiktok.is_country_blacklisted()
        if not is_blacklisted:
            return False

        if self.room_id is None:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED)

        if self.mode == Mode.AUTOMATIC:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE)

        elif self.mode == Mode.FOLLOWERS:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_FOLLOWERS_MODE)

        return is_blacklisted
