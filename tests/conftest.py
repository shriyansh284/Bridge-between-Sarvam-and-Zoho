"""Settings are read at import time, so test settings are set here first."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="ams-test-")
os.environ.update({
    "AMS_NO_DOTENV": "1",  # never read the real .env (keys, addresses) during tests
    "DATA_DIR": _tmp,
    "SARVAM_API_KEY": "test", "SARVAM_ORG_ID": "org", "SARVAM_WORKSPACE_ID": "ws",
    "SARVAM_APP_ID": "Conversatio-test", "SARVAM_APP_VERSION": "1", "SARVAM_CONNECTION_ID": "conn",
    "SARVAM_AGENT_PHONE_NUMBER": "+918000000000",
    "SARVAM_OUTBOUND_BASE": "http://127.0.0.1:19000/api/outbounds",
    "SARVAM_ANALYTICS_BASE": "http://127.0.0.1:19000/api/analytics",
    "ZOHO_ACCOUNTS_URL": "http://127.0.0.1:19100", "ZOHO_MAIL_API_URL": "http://127.0.0.1:19100",
    "ZOHO_CLIENT_ID": "x", "ZOHO_CLIENT_SECRET": "x", "ZOHO_REFRESH_TOKEN": "x",
    "ZOHO_FROM_ADDRESS": "calling@ams.test", "ALLOWED_SENDERS": "@client.test",
    "PUBLIC_BASE_URL": "http://127.0.0.1:18000", "WEBHOOK_SECRET": "hook-secret",
    "CALL_WINDOW_START": "00:00", "CALL_WINDOW_END": "23:59",
    "MAX_ATTEMPTS": "2", "RETRY_GAP_MINUTES": "0", "BACKGROUND_IN_SERVER": "false", "RESULT_POLL_SECONDS": "0",
    "MOCK_RESULT": "digit", "MOCK_DELAY": "0.5",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
