import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher


def test_timeout_after_run_created_no_duplicate(tmp_path, monkeypatch):
    payload = {"schema_version": 1, "observations": [{"project_id": "space-monster", "fresh": True, "facts": ["x"]}]}
    with patch('src.local_watcher.subprocess.run') as mock_run:
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                import subprocess
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=60)
            mock_res = Mock()
            mock_res.returncode = 0
            now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            mock_res.stdout = json.dumps([{"databaseId": 123, "createdAt": now, "status": "queued"}])
            return mock_res
        import subprocess
        mock_run.side_effect = side_effect
        status, info = local_watcher.dispatch_workflow_safe(payload)
        assert status == "accepted"
        assert info is not None


def test_timeout_with_no_run_goes_pending(tmp_path, monkeypatch):
    payload = {"schema_version": 1, "observations": []}
    with patch('src.local_watcher.subprocess.run') as mock_run:
        def side_effect(*args, **kwargs):
            if 'workflow' in str(args[0]) and 'run' in str(args[0]):
                import subprocess
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=60)
            mock_res = Mock()
            mock_res.returncode = 0
            mock_res.stdout = '[]'
            return mock_res
        import subprocess
        mock_run.side_effect = side_effect
        status, info = local_watcher.dispatch_workflow_safe(payload)
        assert status == "no_run"


def test_normal_dispatch_success(tmp_path):
    payload = {"schema_version": 1, "observations": []}
    with patch('src.local_watcher.subprocess.run') as mock_run:
        mock_res = Mock()
        mock_res.returncode = 0
        mock_res.stdout = 'success'
        mock_run.return_value = mock_res
        status, info = local_watcher.dispatch_workflow_safe(payload)
        assert status == "dispatched"
