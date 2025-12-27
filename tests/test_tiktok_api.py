import pytest
from unittest.mock import MagicMock
from utils.enums import TikTokError, StatusCode
from utils.custom_exceptions import UserLiveError, LiveNotFound

class TestTikTokAPI:
    
    def test_is_room_alive_true(self, tiktok_api, mock_http_client):
        """Test is_room_alive returns True when API says alive."""
        # Setup mock response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"alive": True}
            ]
        }
        mock_http_client.get.return_value = mock_response
        
        # Test
        assert tiktok_api.is_room_alive("12345") is True
        
    def test_is_room_alive_false(self, tiktok_api, mock_http_client):
        """Test is_room_alive returns False when API says not alive."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"alive": False}
            ]
        }
        mock_http_client.get.return_value = mock_response
        
        assert tiktok_api.is_room_alive("12345") is False

    def test_get_room_id_from_user_tikrec_success(self, tiktok_api, mock_http_client):
        """Test getting room_id via primary method (tikrec)."""
        # Mock tikrec sign response
        sign_response = MagicMock()
        sign_response.text = '{"signed_path": "/path"}'
        sign_response.json.return_value = {"signed_path": "/path"}
        
        # Mock tikrec data response
        data_response = MagicMock()
        data_response.text = '{"data": {"user": {"roomId": "98765"}}}'
        data_response.json.return_value = {"data": {"user": {"roomId": "98765"}}}
        
        mock_http_client.get.side_effect = [sign_response, data_response]
        
        room_id = tiktok_api.get_room_id_from_user("test_user")
        assert room_id == "98765"

    def test_get_live_url_success(self, tiktok_api, mock_http_client):
        """Test extracting FLV url from room info."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "stream_url": {
                    "flv_pull_url": {
                        "FULL_HD1": "http://test.com/stream.flv"
                    }
                }
            }
        }
        mock_http_client.get.return_value = mock_response
        
        url = tiktok_api.get_live_url("12345")
        assert url == "http://test.com/stream.flv"

    def test_get_live_url_account_private(self, tiktok_api, mock_http_client):
        """Test error when account is private."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "This account is private": True
        }
        mock_http_client.get.return_value = mock_response
        
        with pytest.raises(UserLiveError) as exc:
            tiktok_api.get_live_url("12345")
        assert str(TikTokError.ACCOUNT_PRIVATE) in str(exc.value)
