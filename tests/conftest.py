import sys
import os
import pytest
from unittest.mock import MagicMock

# Add src to python path so we can import modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

@pytest.fixture
def mock_http_client(mocker):
    """Mock the HttpClient to avoid real network requests."""
    # Ensure module is imported so patch can find it
    import core.tiktok_api
    
    mock_client = MagicMock()
    mock_req = MagicMock()
    mock_client.req = mock_req
    
    # Mock the HttpClient class in tiktok_api module
    mocker.patch("core.tiktok_api.HttpClient", return_value=mock_client)
    
    return mock_req

@pytest.fixture
def tiktok_api(mock_http_client):
    """Return an instance of TikTokAPI with mocked http client."""
    from core.tiktok_api import TikTokAPI
    return TikTokAPI(proxy=None, cookies=None)
