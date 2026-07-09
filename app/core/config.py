import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Load environment-based settings for the API service."""

    def __init__(self) -> None:
        self.upstox_api_key = os.getenv("UPSTOX_API_KEY", "")
        self.upstox_api_secret = os.getenv("UPSTOX_API_SECRET", "")
        self.upstox_redirect_url = os.getenv("UPSTOX_REDIRECT_URL", "")
        self.upstox_environment = os.getenv("UPSTOX_ENVIRONMENT", "sandbox")


settings = Settings()
