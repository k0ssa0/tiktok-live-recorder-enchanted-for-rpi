import json
import os
import re
from pathlib import Path
from typing import Optional, Iterator, Dict, Any

from http_utils.http_client import HttpClient
from utils.enums import StatusCode, TikTokError
from utils.logger_manager import logger
from utils.custom_exceptions import (
    UserLiveError,
    TikTokRecorderError,
    LiveNotFound,
    SigningAPIError,
)

# Timeout exceptions to catch
try:
    from requests.exceptions import Timeout, ReadTimeout, ConnectTimeout
except ImportError:
    Timeout = ReadTimeout = ConnectTimeout = Exception

try:
    from curl_cffi.requests.errors import RequestsError
except ImportError:
    RequestsError = Exception


# Room ID cache file - store in user's home to avoid permission issues
ROOM_ID_CACHE_FILE = Path.home() / ".tiktok_recorder_cache.json"


class TikTokAPI:
    """TikTok API client for interacting with live streams."""
    
    # API endpoints
    BASE_URL = "https://www.tiktok.com"
    WEBCAST_URL = "https://webcast.tiktok.com"
    API_URL = "https://www.tiktok.com/api-live/user/room/"
    EULER_API = "https://tiktok.eulerstream.com"
    TIKREC_API = "https://tikrec.com"
    
    # Stream settings
    DEFAULT_STREAM_TIMEOUT = 30
    DEFAULT_CHUNK_SIZE = 4096
    API_TIMEOUT = 30  # Timeout for API calls in seconds

    def __init__(self, proxy: Optional[str], cookies: Optional[Dict[str, str]]):
        self._http_client_obj = HttpClient(proxy, cookies)
        self.http_client = self._http_client_obj.req
        self._http_client_stream = HttpClient(proxy, cookies).req_stream
        self._consecutive_failures = 0
        self._max_failures_before_refresh = 3

    def _safe_get(self, url: str, **kwargs):
        """
        Make a GET request with timeout and error handling.
        Refreshes session after consecutive failures.
        
        Raises:
            TikTokRecorderError: On timeout or network errors after retries
        """
        try:
            # Ensure timeout is set
            if 'timeout' not in kwargs:
                kwargs['timeout'] = self.API_TIMEOUT
            
            response = self.http_client.get(url, **kwargs)
            self._consecutive_failures = 0  # Reset on success
            return response
        except (Timeout, ReadTimeout, ConnectTimeout) as e:
            self._consecutive_failures += 1
            logger.error(f"API request timed out ({self._consecutive_failures}): {e}")
            self._handle_failure()
            raise TikTokRecorderError(f"Request timed out after {self.API_TIMEOUT}s") from e
        except RequestsError as e:
            self._consecutive_failures += 1
            # curl_cffi timeout errors
            if 'timeout' in str(e).lower() or 'timed out' in str(e).lower():
                logger.error(f"API request timed out ({self._consecutive_failures}): {e}")
                self._handle_failure()
                raise TikTokRecorderError(f"Request timed out after {self.API_TIMEOUT}s") from e
            logger.debug(f"API request failed ({self._consecutive_failures}): {e}")
            self._handle_failure()
            raise
        except Exception as e:
            self._consecutive_failures += 1
            logger.debug(f"API request failed ({self._consecutive_failures}): {e}")
            self._handle_failure()
            raise
    
    def _handle_failure(self):
        """Handle consecutive failures by refreshing session if needed."""
        if self._consecutive_failures >= self._max_failures_before_refresh:
            logger.info("Refreshing HTTP session due to consecutive failures...")
            try:
                self._http_client_obj.refresh_session()
                self.http_client = self._http_client_obj.req
            except Exception as e:
                logger.error(f"Failed to refresh session: {e}")
            self._consecutive_failures = 0

    def _is_authenticated(self) -> bool:
        """Check if the current session is authenticated."""
        response = self._safe_get(f"{self.BASE_URL}/foryou")
        response.raise_for_status()
        return "login-title" not in response.text

    def is_country_blacklisted(self) -> bool:
        """Check if the user is in a blacklisted country that requires login."""
        response = self._safe_get(f"{self.BASE_URL}/live", allow_redirects=False)
        return response.status_code == StatusCode.REDIRECT

    def is_room_alive(self, room_id: str) -> bool:
        """
        Check whether the user is currently live.
        
        Args:
            room_id: TikTok room ID to check
            
        Returns:
            True if the room is live, False otherwise
        """
        if not room_id:
            raise UserLiveError(TikTokError.USER_NOT_CURRENTLY_LIVE)

        data = self._safe_get(
            f"{self.WEBCAST_URL}/webcast/room/check_alive/"
            f"?aid=1988&region=CH&room_ids={room_id}&user_is_login=true"
        ).json()

        if "data" not in data or len(data["data"]) == 0:
            return False

        return data["data"][0].get("alive", False)

    def get_sec_uid(self) -> Optional[str]:
        """Returns the sec_uid of the authenticated user."""
        response = self._safe_get(f"{self.BASE_URL}/foryou")

        sec_uid = re.search('"secUid":"(.*?)",', response.text)
        if sec_uid:
            sec_uid = sec_uid.group(1)

        return sec_uid

    def get_user_from_room_id(self, room_id) -> str:
        """
        Given a room_id, I get the username
        """
        data = self._safe_get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
        ).json()

        if "Follow the creator to watch their LIVE" in json.dumps(data):
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE_FOLLOW)

        if "This account is private" in data:
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE)

        display_id = data.get("data", {}).get("owner", {}).get("display_id")
        if display_id is None:
            raise TikTokRecorderError(TikTokError.USERNAME_ERROR)

        return display_id

    def get_room_and_user_from_url(self, live_url: str):
        """
        Given a url, get user and room_id.
        """
        response = self._safe_get(live_url, allow_redirects=False)
        content = response.text
        user = None  # Initialize user

        if response.status_code == StatusCode.REDIRECT:
            raise UserLiveError(TikTokError.COUNTRY_BLACKLISTED)

        if response.status_code == StatusCode.MOVED:  # MOBILE URL
            matches = re.findall("com/@(.*?)/live", content)
            if len(matches) < 1:
                raise LiveNotFound(TikTokError.INVALID_TIKTOK_LIVE_URL)

            user = matches[0]

        # https://www.tiktok.com/@<username>/live
        match = re.match(r"https?://(?:www\.)?tiktok\.com/@([^/]+)/live", live_url)
        if match:
            user = match.group(1)
        
        if not user:
            raise LiveNotFound(TikTokError.INVALID_TIKTOK_LIVE_URL)

        room_id = self.get_room_id_from_user(user)

        return user, room_id

    def _tikrec_get_room_id_signed_url(self, user: str, max_retries: int = 10) -> str:
        """Get signed URL from tikrec API with automatic retry on blocked responses or errors."""
        import random
        import time
        
        last_error = None
        
        for attempt in range(max_retries):
            try:
                response = self._safe_get(
                    f"{self.TIKREC_API}/tiktok/room/api/sign",
                    params={"unique_id": user},
                )

                # Check response content before parsing JSON
                content = response.text
                if not content:
                    raise SigningAPIError(f"Empty response from signing API for user: {user}")
                
                # Check for HTML error pages or rate limiting (Cloudflare block)
                if content.strip().startswith('<') or 'Please wait' in content or '<!DOCTYPE' in content:
                    logger.debug(f"Signing API returned non-JSON response: {content[:200]}...")
                    raise SigningAPIError(f"Signing API returned HTML/blocked response for user: {user}")
                
                try:
                    data = response.json()
                except Exception as e:
                    logger.debug(f"Failed to parse JSON from signing API: {content[:200]}...")
                    raise SigningAPIError(f"Invalid JSON from signing API for user: {user}") from e

                signed_path = data.get("signed_path")
                if not signed_path:
                    raise TikTokRecorderError(f"Failed to get signed URL for user: {user}")
                return f"{self.BASE_URL}{signed_path}"
                
            except (SigningAPIError, TikTokRecorderError) as e:
                # Retry with 5-10 second delay for signing API blocks or timeouts
                last_error = e
                wait_time = random.uniform(5, 10)
                logger.warning(f"Tikrec API error (attempt {attempt + 1}/{max_retries}): Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
            except Exception as e:
                # Catch any other unexpected errors and retry
                last_error = e
                wait_time = random.uniform(5, 10)
                logger.warning(f"Tikrec API error (attempt {attempt + 1}/{max_retries}): Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
        
        # If we exhausted all retries, raise the error
        raise SigningAPIError(f"Tikrec API failed after {max_retries} attempts. Last error: {last_error}")

    def _tikrec_get_room_id(self, user: str) -> str | None:
        """Get room_id using tikrec signed URL method."""
        signed_url = self._tikrec_get_room_id_signed_url(user)
        response = self._safe_get(signed_url)
        content = response.text

        if not content or "Please wait" in content:
            raise UserLiveError(TikTokError.WAF_BLOCKED)

        # Check for HTML/blocked responses before parsing JSON
        if content.strip().startswith('<') or '<!DOCTYPE' in content:
            logger.debug(f"Tikrec returned HTML response for user {user}: {content[:200]}...")
            raise SigningAPIError(f"Tikrec returned HTML/blocked response for user: {user}")

        try:
            data = response.json()
        except Exception as e:
            logger.debug(f"Failed to parse JSON from tikrec for user {user}: {content[:200]}...")
            raise SigningAPIError(f"Invalid JSON from tikrec for user: {user}") from e
            
        return (data.get("data") or {}).get("user", {}).get("roomId")

    def _euler_get_room_id(self, user: str, max_retries: int = 10) -> str | None:
        """Get room_id using EulerStream API with retries."""
        import random
        import time
        
        last_error = None
        
        for attempt in range(max_retries):
            try:
                params = {"uniqueId": user, "giftInfo": "false"}
                response = self._safe_get(
                    f"{self.EULER_API}/webcast/room_info",
                    params=params,
                    headers={"x-api-key": ""},
                )

                if response.status_code != 200:
                    raise SigningAPIError(f"EulerStream returned status {response.status_code}")

                content = response.text
                if not content:
                    raise SigningAPIError("Empty response from EulerStream")
                
                # Check for HTML/blocked responses
                if content.strip().startswith('<') or '<!DOCTYPE' in content:
                    logger.debug(f"EulerStream returned non-JSON response: {content[:200]}...")
                    raise SigningAPIError("EulerStream returned HTML/blocked response")

                data = response.json()
                room_id = data.get("data", {}).get("room_info", {}).get("id")
                if room_id:
                    return room_id
                    
                # No room_id means user is not live - this is valid, return None
                return None
                
            except SigningAPIError as e:
                last_error = e
                wait_time = random.uniform(5, 10)
                logger.warning(f"EulerStream API error (attempt {attempt + 1}/{max_retries}): Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
            except Exception as e:
                last_error = e
                wait_time = random.uniform(5, 10)
                logger.warning(f"EulerStream API error (attempt {attempt + 1}/{max_retries}): Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
        
        raise SigningAPIError(f"EulerStream API failed after {max_retries} attempts. Last error: {last_error}")

    @staticmethod
    def cache_room_id(user: str, room_id: str):
        """Cache room_id for a user to a file."""
        try:
            cache = {}
            if ROOM_ID_CACHE_FILE.exists():
                with open(ROOM_ID_CACHE_FILE, 'r') as f:
                    cache = json.load(f)
            
            cache[user.lower()] = {
                "room_id": room_id,
                "updated": str(json.dumps({"t": __import__('datetime').datetime.now().isoformat()}))
            }
            
            with open(ROOM_ID_CACHE_FILE, 'w') as f:
                json.dump(cache, f, indent=2)
            logger.debug(f"Cached room_id {room_id} for user {user}")
        except Exception as e:
            logger.debug(f"Failed to cache room_id: {e}")

    @staticmethod
    def get_cached_room_id(user: str) -> str | None:
        """Get cached room_id for a user."""
        try:
            if not ROOM_ID_CACHE_FILE.exists():
                return None
            with open(ROOM_ID_CACHE_FILE, 'r') as f:
                cache = json.load(f)
            data = cache.get(user.lower(), {})
            return data.get("room_id")
        except Exception as e:
            logger.debug(f"Failed to read cached room_id: {e}")
            return None

    @staticmethod
    def clear_cached_room_id(user: str = None):
        """Clear cached room_id for a user or all users."""
        try:
            if user is None:
                # Clear all
                if ROOM_ID_CACHE_FILE.exists():
                    ROOM_ID_CACHE_FILE.unlink()
                    logger.info("Cleared all cached room IDs")
            else:
                if ROOM_ID_CACHE_FILE.exists():
                    with open(ROOM_ID_CACHE_FILE, 'r') as f:
                        cache = json.load(f)
                    if user.lower() in cache:
                        del cache[user.lower()]
                        with open(ROOM_ID_CACHE_FILE, 'w') as f:
                            json.dump(cache, f, indent=2)
                        logger.info(f"Cleared cached room ID for {user}")
        except Exception as e:
            logger.debug(f"Failed to clear cached room_id: {e}")

    def get_room_id_from_user(self, user: str) -> str | None:
        """
        Get room_id for a user with fallback chain:
        1. Try tikrec.com (10 retries)
        2. If fails, try EulerStream (10 retries)
        3. If all fail, try cached room_id
        """
        room_id = None
        
        # Method 1: Try tikrec
        try:
            logger.debug("Trying tikrec API...")
            room_id = self._tikrec_get_room_id(user)
            if room_id:
                self.cache_room_id(user, room_id)
                return room_id
            return room_id  # Could be None if user not live
        except (SigningAPIError, TikTokRecorderError) as e:
            logger.warning(f"Tikrec failed, switching to EulerStream: {e}")
        
        # Method 2: Try EulerStream
        try:
            logger.info("Trying EulerStream API as fallback...")
            room_id = self._euler_get_room_id(user)
            if room_id:
                self.cache_room_id(user, room_id)
                return room_id
            return room_id  # Could be None if user not live
        except (SigningAPIError, TikTokRecorderError) as e:
            logger.warning(f"EulerStream also failed: {e}")
        
        # Method 3: Try cached room_id
        cached_room_id = self.get_cached_room_id(user)
        if cached_room_id:
            logger.info(f"Using cached room_id: {cached_room_id}")
            return cached_room_id
        
        # All methods failed
        raise SigningAPIError(f"All methods to get room_id failed for user: {user}")

    def get_followers_list(self, sec_uid) -> list:
        """
        Returns all followers for the authenticated user by paginating
        """
        followers = []
        cursor = 0
        has_more = True

        ms_token = self._safe_get(
            f"{self.BASE_URL}/api/user/list/?"
            "WebIdLastTime=1747672102&aid=1988&app_language=it-IT&app_name=tiktok_web&"
            "browser_language=it-IT&browser_name=Mozilla&browser_online=true&"
            "browser_platform=Linux%20x86_64&"
            "browser_version=5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F140.0.0.0%20Safari%2F537.36&"
            "channel=tiktok_web&cookie_enabled=true&count=5&data_collection_enabled=true&"
            "device_id=7506194516308166166&device_platform=web_pc&focus_state=true&"
            "from_page=user&history_len=3&is_fullscreen=false&is_page_visible=true&"
            "maxCursor=0&minCursor=0&odinId=7246312836442604570&os=linux&priority_region=IT&"
            "referer=&region=IT&root_referer=https%3A%2F%2Fwww.tiktok.com%2Flive&scene=21&"
            "screen_height=1080&screen_width=1920&tz_name=Europe%2FRome&user_is_login=true&"
            "verifyFp=verify_mh4yf0uq_rdjp1Xwt_OoTk_4Jrf_AS8H_sp31opbnJFre&webcast_language=it-IT&"
            "msToken=GphHoLvRR4QxA5AWVwDkrs3AbumoK5H8toE8LVHtj6cce3ToGdXhMfvDWzOXG-0GXUWoaGVHrwGNA4k_NnjuFFnHgv2S5eMjsvtkAhwMPa13xLmvP7tumx0KreFjPwTNnOj-BvAkPdO5Zrev3hoFBD9lHVo=&X-Bogus=&X-Gnarly="
        ).cookies["msToken"]

        while has_more:
            url = (
                "https://www.tiktok.com/api/user/list/?"
                "WebIdLastTime=1747672102&aid=1988&app_language=it-IT&app_name=tiktok_web"
                "&browser_language=it-IT&browser_name=Mozilla&browser_online=true"
                "&browser_platform=Linux%20x86_64&browser_version=5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F140.0.0.0%20Safari%2F537.36&channel=tiktok_web&"
                "cookie_enabled=true&count=5&data_collection_enabled=true&device_id=7506194516308166166"
                "&device_platform=web_pc&focus_state=true&from_page=user&history_len=3&"
                f"is_fullscreen=false&is_page_visible=true&maxCursor={cursor}&minCursor={cursor}&"
                "odinId=7246312836442604570&os=linux&priority_region=IT&referer=&"
                "region=IT&scene=21&screen_height=1080&screen_width=1920"
                "&tz_name=Europe%2FRome&user_is_login=true&"
                f"secUid={sec_uid}&verifyFp=verify_mh4yf0uq_rdjp1Xwt_OoTk_4Jrf_AS8H_sp31opbnJFre&"
                f"webcast_language=it-IT&msToken={ms_token}&X-Bogus=&X-Gnarly="
            )

            response = self._safe_get(url)

            if response.status_code != StatusCode.OK:
                raise TikTokRecorderError("Failed to retrieve followers list.")

            data = response.json()
            user_list = data.get("userList", [])

            for user in user_list:
                username = user.get("user", {}).get("uniqueId")
                if username:
                    followers.append(username)

            has_more = data.get("hasMore", False)
            new_cursor = data.get("minCursor", 0)

            if new_cursor == cursor:
                break

            cursor = new_cursor

        if not followers:
            raise TikTokRecorderError("Followers list is empty.")

        return followers

    def get_live_url(self, room_id: str) -> str | None:
        """
        Return the cdn (flv or m3u8) of the streaming
        """
        data = self._safe_get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
        ).json()

        if "This account is private" in data:
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE)

        stream_url = data.get("data", {}).get("stream_url", {})

        sdk_data_str = (
            stream_url.get("live_core_sdk_data", {})
            .get("pull_data", {})
            .get("stream_data")
        )
        if not sdk_data_str:
            logger.warning(
                "No SDK stream data found. Falling back to legacy URLs. Consider contacting the developer to update the code."
            )
            return (
                stream_url.get("flv_pull_url", {}).get("FULL_HD1")
                or stream_url.get("flv_pull_url", {}).get("HD1")
                or stream_url.get("flv_pull_url", {}).get("SD2")
                or stream_url.get("flv_pull_url", {}).get("SD1")
                or stream_url.get("rtmp_pull_url", "")
            )

        # Extract stream options
        sdk_data = json.loads(sdk_data_str).get("data", {})
        qualities = (
            stream_url.get("live_core_sdk_data", {})
            .get("pull_data", {})
            .get("options", {})
            .get("qualities", [])
        )
        if not qualities:
            logger.warning("No qualities found in the stream data. Returning None.")
            return None
        level_map = {q["sdk_key"]: q["level"] for q in qualities}

        best_level = -1
        best_flv = None
        for sdk_key, entry in sdk_data.items():
            level = level_map.get(sdk_key, -1)
            stream_main = entry.get("main", {})
            if level > best_level:
                best_level = level
                best_flv = stream_main.get("flv")

        if not best_flv and data.get("status_code") == 4003110:
            raise UserLiveError(TikTokError.LIVE_RESTRICTION)

        return best_flv

    def download_live_stream(
        self, 
        live_url: str, 
        timeout: int = DEFAULT_STREAM_TIMEOUT,
        chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> Iterator[bytes]:
        """
        Generator that yields chunks from a live stream.
        
        Args:
            live_url: The URL to the live stream
            timeout: Connection timeout in seconds
            chunk_size: Size of chunks to yield
            
        Yields:
            Bytes chunks from the stream
        """
        stream = self._http_client_stream.get(live_url, stream=True, timeout=timeout)
        for chunk in stream.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
