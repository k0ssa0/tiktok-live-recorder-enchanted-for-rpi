import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.enums import StatusCode
from utils.logger_manager import logger
from utils.utils import is_termux


class HttpClient:
    # Default timeouts (connect, read) in seconds
    DEFAULT_TIMEOUT = (10, 30)
    STREAM_TIMEOUT = (10, 60)
    
    def __init__(self, proxy=None, cookies=None):
        self.req = None
        self.req_stream = None

        self.proxy = proxy
        self.cookies = cookies
        self.headers = {
            "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Accept-Language": "en-US",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,application/json,text/plain,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Priority": "u=0, i",
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
            "Connection": "keep-alive",
        }

        self.configure_session()

    def configure_session(self) -> None:
        # Configure stream session with retry logic
        self.req_stream = requests.Session()
        
        # Add retry strategy for resilience
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy, 
            pool_connections=10, 
            pool_maxsize=10,
            pool_block=False
        )
        self.req_stream.mount("http://", adapter)
        self.req_stream.mount("https://", adapter)

        if is_termux():
            self.req = self.req_stream
        else:
            from curl_cffi import Session, CurlSslVersion, CurlOpt

            self.req = Session(
                impersonate="chrome136",
                http_version="v1",
                timeout=self.DEFAULT_TIMEOUT[1],  # Use read timeout (30s)
                curl_options={CurlOpt.SSLVERSION: CurlSslVersion.TLSv1_2},
            )

        self.req.headers.update(self.headers)
        self.req_stream.headers.update(self.headers)

        if self.cookies is not None:
            self.req.cookies.update(self.cookies)
            self.req_stream.cookies.update(self.cookies)

        self.check_proxy()
    
    def refresh_session(self) -> None:
        """Refresh HTTP sessions to prevent stale connections."""
        logger.debug("Refreshing HTTP sessions...")
        try:
            if self.req_stream:
                self.req_stream.close()
        except Exception:
            pass
        self.configure_session()
        logger.debug("HTTP sessions refreshed")

    def check_proxy(self) -> None:
        if self.proxy is None:
            return

        logger.info(f"Testing proxy: {self.proxy}...")
        proxies = {"http": self.proxy, "https": self.proxy}

        try:
            response = requests.get("https://ifconfig.me/ip", proxies=proxies, timeout=10)

            if response.status_code == StatusCode.OK:
                self.req.proxies.update(proxies)
                self.req_stream.proxies.update(proxies)
                logger.info(f"Proxy set up successfully. External IP: {response.text.strip()}")
            else:
                logger.warning(f"Proxy test returned status {response.status_code}")
        except requests.RequestException as e:
            logger.error(f"Proxy test failed: {e}")
