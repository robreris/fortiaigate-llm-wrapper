from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str
    mcp_server_url: str
    default_model: str = "gpt-4o"
    mcp_require_approval: str = "never"
    log_level: str = "INFO"
    port: int = 8080

    model_config = {"env_file": ".env"}


settings = Settings()
