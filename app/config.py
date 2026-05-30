from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    wecom_connection_mode: str = "long_connection"
    wecom_aibot_bot_id: str = ""
    wecom_aibot_secret: str = ""
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_token: str = ""
    wecom_aes_key: str = ""
    wecom_kf_secret: str = Field(default="", alias="WECOM_KF_SECRET")
    wecom_kf_token: str = Field(default="", alias="WECOM_KF_TOKEN")
    wecom_kf_aes_key: str = Field(default="", alias="WECOM_KF_AES_KEY")
    wecom_kf_state_path: Path = Path("data/wecom_kf_state.json")
    wecom_kf_context_path: Path = Path("data/wecom_kf_context.json")
    wecom_kf_sync_limit: int = 100
    wecom_kf_sync_max_pages: int = 3
    wecom_kf_satisfaction_delay_seconds: int = 300
    wecom_kf_satisfaction_prompt: str = (
        "\u8fd9\u6b21\u56de\u590d\u6709\u5e2e\u5230\u4f60\u5417\uff1f"
        "\u6ee1\u610f\u7684\u8bdd\u56de\u201c\u6ee1\u610f\u201d\uff0c"
        "\u4e0d\u6ee1\u610f\u76f4\u63a5\u544a\u8bc9\u6211\u54ea\u91cc\u6ca1\u89e3\u51b3\uff0c\u6211\u9a6c\u4e0a\u6539\u3002"
    )

    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    dashscope_model: str = Field(default="qwen3.5-omni-plus", alias="DASHSCOPE_MODEL")
    dashscope_vision_model: str = Field(default="qwen3.5-omni-plus", alias="DASHSCOPE_VISION_MODEL")

    inventory_source: str = "local_image"
    kdocs_public_url: str = ""
    inventory_cache_path: Path = Path("data/inventory_cache.csv")
    inventory_image_cache_path: Path = Path("data/inventory_image_cache.md")
    inventory_refresh_seconds: int = 300
    room_database_path: Path = Path("room_database")
    inventory_image_path: Path = Path("room_database/inventory.png")
    inventory_image_glob: str = "room_database/inventory_*.png"

    feishu_app_id: str = Field(default="", alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field(default="", alias="FEISHU_APP_SECRET")
    feishu_bitable_app_token: str = Field(default="", alias="FEISHU_BITABLE_APP_TOKEN")
    feishu_bitable_table_id: str = Field(default="", alias="FEISHU_BITABLE_TABLE_ID")
    feishu_bitable_view_id: str = Field(default="", alias="FEISHU_BITABLE_VIEW_ID")
    feishu_drive_root_folder_token: str = Field(
        default="",
        alias="FEISHU_DRIVE_ROOT_FOLDER_TOKEN",
    )
    feishu_sync_media_on_startup: bool = Field(
        default=False,
        alias="FEISHU_SYNC_MEDIA_ON_STARTUP",
    )
    feishu_inventory_sheet_token: str = Field(
        default="",
        alias="FEISHU_INVENTORY_SHEET_TOKEN",
    )
    feishu_inventory_sheet_sync_on_startup: bool = Field(
        default=False,
        alias="FEISHU_INVENTORY_SHEET_SYNC_ON_STARTUP",
    )
    feishu_inventory_sheet_check_seconds: int = Field(
        default=300,
        alias="FEISHU_INVENTORY_SHEET_CHECK_SECONDS",
    )
    inventory_image_sync_state_path: Path = Field(
        default=Path("data/inventory_image_sync_state.json"),
        alias="INVENTORY_IMAGE_SYNC_STATE_PATH",
    )

    media_root: Path = Path("media/rooms")

    require_inventory_grounding: bool = True
    default_fallback_reply: str = (
        "\u6211\u5148\u5e2e\u60a8\u786e\u8ba4\u4e00\u4e0b\u6700\u65b0\u623f\u6001"
        "\uff0c\u7a0d\u540e\u7ed9\u60a8\u51c6\u786e\u56de\u590d\u3002"
    )


settings = Settings()
