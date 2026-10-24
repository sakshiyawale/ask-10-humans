"""All settings come from environment variables (a local .env file is loaded if present)."""
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional in production
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


DATABASE_PATH = os.getenv("DATABASE_PATH", "ask10.db")
DECISIONS_LOG_PATH = os.getenv("DECISIONS_LOG_PATH", "decisions.log")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Whop
WHOP_BUSINESS_ID = os.getenv("WHOP_BUSINESS_ID", "biz_cNH3PPb4JRGlkd")
WHOP_WEBHOOK_SECRET = os.getenv("WHOP_WEBHOOK_SECRET", "")
WHOP_API_KEY = os.getenv("WHOP_API_KEY", "")
WHOP_API_BASE = os.getenv("WHOP_API_BASE", "https://api.whop.com/api/v1").rstrip("/")
WEBHOOK_TOLERANCE_SECONDS = int(os.getenv("WEBHOOK_TOLERANCE_SECONDS", "300"))

# Terac
TERAC_API_KEY = os.getenv("TERAC_API_KEY", "")
TERAC_MCP_URL = os.getenv("TERAC_MCP_URL", "https://terac.com/api/mcp")
TERAC_CALLBACK_URL = os.getenv("TERAC_CALLBACK_URL", "https://terac.com/api/external/callback")
TERAC_PROJECT_ID = os.getenv("TERAC_PROJECT_ID", "")
TERAC_AUTO_LAUNCH = _bool("TERAC_AUTO_LAUNCH", True)
PANEL_SIZE = int(os.getenv("PANEL_SIZE", "10"))
TASK_MINUTES = int(os.getenv("TASK_MINUTES", "2"))

# LLM (Anthropic)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Email (Resend)
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "Ask 10 Humans <onboarding@resend.dev>")

# CEO policy
PRICE_USD = float(os.getenv("PRICE_USD", "9"))
REFUND_AFTER_MINUTES = int(os.getenv("REFUND_AFTER_MINUTES", "60"))
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
CEO_AUTO_PRICING = _bool("CEO_AUTO_PRICING", False)
TARGET_MARGIN = float(os.getenv("TARGET_MARGIN", "0.3"))

# Testing only: accept votes that don't carry a Terac submission id.
ALLOW_ANON_VOTES = _bool("ALLOW_ANON_VOTES", False)
