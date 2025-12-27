import unittest
from unittest.mock import MagicMock, patch
from src.core.tiktok_api import TikTokAPI
from src.utils.video_management import VideoManagement
import os
from pathlib import Path

class TestM3U8Logic(unittest.TestCase):
    def setUp(self):
        self.api = TikTokAPI(proxy=None, cookies=None)

    def test_is_m3u8_url(self):
        self.assertTrue(self.api.is_m3u8_url("http://example.com/stream.m3u8"))
        self.assertTrue(self.api.is_m3u8_url("http://example.com/stream/index.m3u8?query=1"))
        self.assertTrue(self.api.is_m3u8_url("http://example.com/hls/stream.ts"))
        self.assertFalse(self.api.is_m3u8_url("http://example.com/stream.flv"))

    @patch('src.core.tiktok_api.HttpClient')
    def test_download_m3u8_stream(self, mock_http_client):
        # Mock the requests
        mock_req = MagicMock()
        self.api._http_client_stream = mock_req

        master_playlist = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
http://example.com/variant_v1.m3u8
"""
        variant_playlist_1 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXTINF:2.0,
segment1.ts
#EXTINF:2.0,
segment2.ts
"""
        segment_content = b"fake_video_data"

        def side_effect(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "master.m3u8" in url:
                resp.text = master_playlist
            elif "variant_v1.m3u8" in url:
                resp.text = variant_playlist_1
            elif ".ts" in url:
                resp.iter_content = lambda chunk_size: [segment_content]
            return resp

        mock_req.get.side_effect = side_effect

        chunks = list(self.api.download_m3u8_stream("http://example.com/master.m3u8", poll_interval=0.1))
        self.assertTrue(len(chunks) > 0)

    @patch('src.utils.video_management.ffmpeg')
    @patch('src.utils.video_management.VideoManagement.get_file_size_mb')
    @patch('src.utils.video_management.VideoManagement.wait_for_file_release')
    @patch('src.utils.video_management.shutil')
    @patch('builtins.open')  # Mock open to prevent file creation
    def test_convert_filename_logic(self, mock_open, mock_shutil, mock_wait, mock_size, mock_ffmpeg):
        # Setup mocks
        mock_wait.return_value = True
        mock_size.return_value = 10.0
        
        # Test 1: FLV file
        flv_file = "videos/test_flv.mp4"
        expected_flv = "videos/test.mp4"
        
        # We only care about the filename calculation here, so we look at the calls to ffmpeg
        VideoManagement.convert_flv_to_mp4(flv_file)
        
        # Check that ffmpeg.input was called with the correct output file in the chain
        # ffmpeg.input().output(OUTPUT_FILE, ...)
        # It's a bit hard to mock the fluent interface perfectly, so we'll check the call arguments if possible
        # Or better, we can trust the return value of our modified function if we mock the chain to return it.
        
        # Actually, let's just inspect the code change via a simpler unit test on a helper if it existed,
        # but since we are testing the whole method:
        
        # The method returns the output path on success.
        # We need to mock the ffmpeg().output().run() chain to not fail.
        mock_stream = MagicMock()
        mock_ffmpeg.input.return_value = mock_stream
        mock_stream.output.return_value = mock_stream
        mock_stream.run.return_value = None
        
        # FLV
        res_flv = VideoManagement.convert_flv_to_mp4(flv_file)
        self.assertEqual(res_flv, expected_flv)

        # Test 2: M3U8 TS file
        ts_file = "videos/test_hls.ts"
        expected_ts = "videos/test.mp4"
        
        res_ts = VideoManagement.convert_flv_to_mp4(ts_file)
        self.assertEqual(res_ts, expected_ts)

        # Test 3: Generic TS file
        generic_ts = "videos/stream.ts"
        expected_generic = "videos/stream.mp4"
        
        res_generic = VideoManagement.convert_flv_to_mp4(generic_ts)
        self.assertEqual(res_generic, expected_generic)

if __name__ == '__main__':
    unittest.main()