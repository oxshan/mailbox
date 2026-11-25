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
        for key, value in overrides.items():
            setattr(self, key, value)
