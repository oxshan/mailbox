"""Runtime settings, read from the environment."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


class Config:
    def __init__(self, **overrides):
        env = os.environ.get
        self.domains = [d.strip().lower() for d in env("MAIL_DOMAINS", "").split(",") if d.strip()]
        self.api_key = env("API_KEY", "")
        self.smtp_host = env("SMTP_HOST", "0.0.0.0")
        self.smtp_port = int(env("SMTP_PORT", "25"))
        self.http_host = env("HTTP_HOST", "0.0.0.0")
        self.http_port = int(env("HTTP_PORT", "8080"))
        self.data_dir = env("DATA_DIR", os.path.join(HERE, "data"))
        self.static_dir = env("STATIC_DIR", os.path.join(HERE, "..", "frontend", "dist"))
        self.retention_hours = float(env("RETENTION_HOURS", "24"))
        self.max_mail_bytes = int(env("MAX_MAIL_BYTES", str(2 * 1024 * 1024)))
        self.mx_check_port = 25
        self.public_ip = env("PUBLIC_IP", "").strip()
        self.resend_api_key = env("RESEND_API_KEY", "")
        self.resend_api_url = env("RESEND_API_URL", "https://api.resend.com/emails")
        self.forward_hourly_limit = int(env("FORWARD_HOURLY_LIMIT", "60"))
        self.addy_api_url = env("ADDY_API_URL", "https://app.addy.io/api/v1")
        self.duck_api_url = env("DUCK_API_URL", "https://quack.duckduckgo.com/api")
        self.ms_token_url = env("MS_TOKEN_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
        self.ms_imap_token_url = env(
            "MS_IMAP_TOKEN_URL", "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
        )
        self.ms_graph_url = env("MS_GRAPH_URL", "https://graph.microsoft.com/v1.0")
        self.ms_device_url = env("MS_DEVICE_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode")
        # Thunderbird's public application id, which Microsoft lets sign in with a device code.
        self.ms_client_id = env("MS_CLIENT_ID", "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
        self.ms_authorize_url = env("MS_AUTHORIZE_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/authorize")
        # Where Microsoft sends the browser after signing in. Nothing needs to answer there: the user copies
        # the address from the browser's address bar. A desktop app such as Thunderbird may use any localhost port.
        self.ms_redirect_uri = env("MS_REDIRECT_URI", "http://localhost:8080")
        self.outlook_imap_host = env("OUTLOOK_IMAP_HOST", "outlook.live.com")
        self.account_sync_minutes = float(env("ACCOUNT_SYNC_MINUTES", "5"))
        self.account_sync_min_seconds = 10
        for key, value in overrides.items():
            setattr(self, key, value)
