import os
import json
import pytest
from unittest.mock import MagicMock, patch
from utils.session_manager import SessionManager

class TestSessionManager:
    
    @pytest.fixture
    def session_mgr(self, tmp_path):
        """Fixture for SessionManager with temp files."""
        # Use a temporary file path for testing
        test_session_file = tmp_path / "test_session.json"
        test_command_file = tmp_path / "test_command"
        
        mgr = SessionManager()
        mgr.session_file = str(test_session_file)
        mgr.command_file = str(test_command_file)
        return mgr

    def test_start_session(self, session_mgr):
        """Test creating a new session file."""
        session_mgr.start_session("test_user", "test.log")
        
        assert os.path.exists(session_mgr.session_file)
        
        with open(session_mgr.session_file, 'r') as f:
            data = json.load(f)
            
        assert data['user'] == "test_user"
        assert data['state'] == "starting"
        assert data['log_file'] == "test.log"
        
        # Cleanup
        session_mgr.end_session()
        assert not os.path.exists(session_mgr.session_file)

    def test_send_and_read_command(self, session_mgr):
        """Test sending and reading IPC commands."""
        # Send command
        session_mgr.send_command("status")
        assert os.path.exists(session_mgr.command_file)
        
        # Read command
        cmd = session_mgr.read_command()
        assert cmd == "status"
        
        # File should be gone after reading
        assert not os.path.exists(session_mgr.command_file)
        
        # Reading again should return None
        assert session_mgr.read_command() is None

    @patch('os.kill')
    def test_check_existing_session_alive(self, mock_kill, session_mgr):
        """Test detecting an alive session."""
        # Mock os.kill to not raise error (process exists)
        mock_kill.return_value = None
        
        # Create a fake session file
        with open(session_mgr.session_file, 'w') as f:
            json.dump({'pid': 12345, 'user': 'alive_user'}, f)
            
        session = session_mgr.check_existing_session()
        assert session is not None
        assert session['user'] == 'alive_user'

    @patch('os.kill')
    def test_check_existing_session_dead(self, mock_kill, session_mgr):
        """Test detecting and cleaning up a dead session."""
        # Mock os.kill to raise OSError (process does not exist)
        mock_kill.side_effect = OSError
        
        with open(session_mgr.session_file, 'w') as f:
            json.dump({'pid': 99999, 'user': 'dead_user'}, f)
            
        session = session_mgr.check_existing_session()
        assert session is None
        # File should be cleaned up
        assert not os.path.exists(session_mgr.session_file)
