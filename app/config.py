from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str
    mcp_server_url: str
    mcp_server_label: str = "context7"
    default_model: str = "gpt-4o"
    mcp_require_approval: str = "never"
    mcp_api_key: str | None = None
    log_level: str = "INFO"

    model_config = {"env_file": ".env"}


settings = Settings()
