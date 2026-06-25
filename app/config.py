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
    wecom_kf_welcome_interval_seconds: int = Field(
        default=600,
        alias="WECOM_KF_WELCOME_INTERVAL_SECONDS",
    )
    wecom_kf_welcome_text: str = Field(
        default=(
            "你好，我在。找房、要视频、看密码、发房源表都可以直接说。\n\n"
            "比如：\n"
            "“万达1500左右还有哪些？”\n"
            "“新天地4000左右两室有吗？”\n"
            "“这套视频发我”\n"
            "“今天能看吗，密码多少？”\n"
            "“房源表发我一下”\n\n"
            "小区名、房号记不清也没事，发个大概我来帮你对。"
        ),
        alias="WECOM_KF_WELCOME_TEXT",
    )
    wecom_kf_satisfaction_delay_seconds: int = 300
    wecom_kf_satisfaction_prompt: str = (
        "\u8fd9\u6b21\u56de\u590d\u6709\u5e2e\u5230\u4f60\u5417\uff1f"
        "\u6ee1\u610f\u7684\u8bdd\u56de\u201c\u6ee1\u610f\u201d\uff0c"
        "\u4e0d\u6ee1\u610f\u76f4\u63a5\u544a\u8bc9\u6211\u54ea\u91cc\u6ca1\u89e3\u51b3\uff0c\u6211\u9a6c\u4e0a\u6539\u3002"
    )
    kf_issue_collection_enabled: bool = Field(
        default=False,
        alias="KF_ISSUE_COLLECTION_ENABLED",
    )
    kf_dialogue_event_log_path: Path = Field(
        default=Path("data/kf_dialogue_events.jsonl"),
        alias="KF_DIALOGUE_EVENT_LOG_PATH",
    )
    kf_issue_candidate_path: Path = Field(
        default=Path("data/kf_issue_candidates.jsonl"),
        alias="KF_ISSUE_CANDIDATE_PATH",
    )
    kf_issue_state_path: Path = Field(
        default=Path("data/kf_issue_state.json"),
        alias="KF_ISSUE_STATE_PATH",
    )
    kf_good_case_candidate_path: Path = Field(
        default=Path("data/kf_good_case_candidates.jsonl"),
        alias="KF_GOOD_CASE_CANDIDATE_PATH",
    )
    kf_good_case_state_path: Path = Field(
        default=Path("data/kf_good_case_state.json"),
        alias="KF_GOOD_CASE_STATE_PATH",
    )
    kf_issue_realtime_until: str = Field(
        default="",
        alias="KF_ISSUE_REALTIME_UNTIL",
    )
    kf_issue_daily_summary_enabled: bool = Field(
        default=True,
        alias="KF_ISSUE_DAILY_SUMMARY_ENABLED",
    )
    kf_issue_public_token: str = Field(
        default="",
        alias="KF_ISSUE_PUBLIC_TOKEN",
    )
    kf_dialogue_training_dir: Path = Field(
        default=Path("ml_artifacts/dialogue"),
        alias="KF_DIALOGUE_TRAINING_DIR",
    )
    kf_dialogue_raw_candidates_path: Path = Field(
        default=Path("ml_artifacts/dialogue/dialogue_raw_candidates.jsonl"),
        alias="KF_DIALOGUE_RAW_CANDIDATES_PATH",
    )
    kf_dialogue_reviewed_gold_path: Path = Field(
        default=Path("ml_artifacts/dialogue/dialogue_reviewed_gold.jsonl"),
        alias="KF_DIALOGUE_REVIEWED_GOLD_PATH",
    )
    kf_dialogue_rejected_path: Path = Field(
        default=Path("ml_artifacts/dialogue/dialogue_rejected.jsonl"),
        alias="KF_DIALOGUE_REJECTED_PATH",
    )
    kf_dialogue_uncertain_path: Path = Field(
        default=Path("ml_artifacts/dialogue/dialogue_uncertain.jsonl"),
        alias="KF_DIALOGUE_UNCERTAIN_PATH",
    )
    kf_dialogue_sft_train_path: Path = Field(
        default=Path("ml_artifacts/dialogue/dialogue_sft_train.jsonl"),
        alias="KF_DIALOGUE_SFT_TRAIN_PATH",
    )
    kf_dialogue_eval_cases_path: Path = Field(
        default=Path("ml_artifacts/dialogue/dialogue_eval_cases.jsonl"),
        alias="KF_DIALOGUE_EVAL_CASES_PATH",
    )
    kf_agentic_rag_enabled: bool = Field(
        default=True,
        alias="KF_AGENTIC_RAG_ENABLED",
    )
    kf_agentic_rag_knowledge_dir: Path = Field(
        default=Path("knowledge/kf"),
        alias="KF_AGENTIC_RAG_KNOWLEDGE_DIR",
    )
    kf_agentic_rag_max_evidence: int = Field(
        default=5,
        alias="KF_AGENTIC_RAG_MAX_EVIDENCE",
    )

    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_rewrite_model: str = Field(
        default="deepseek-chat",
        alias="DEEPSEEK_REWRITE_MODEL",
    )
    deepseek_planner_model: str = Field(
        default="deepseek-chat",
        alias="DEEPSEEK_PLANNER_MODEL",
    )
    deepseek_reply_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_REPLY_MODEL")
    deepseek_selfcheck_model: str = Field(
        default="deepseek-chat",
        alias="DEEPSEEK_SELFCHECK_MODEL",
    )
    deepseek_retry_model: str = Field(
        default="deepseek-reasoner",
        alias="DEEPSEEK_RETRY_MODEL",
    )
    llm_rewrite_provider: str = Field(default="dashscope", alias="LLM_REWRITE_PROVIDER")
    llm_planner_provider: str = Field(default="dashscope", alias="LLM_PLANNER_PROVIDER")
    llm_reply_provider: str = Field(default="dashscope", alias="LLM_REPLY_PROVIDER")
    llm_selfcheck_provider: str = Field(default="dashscope", alias="LLM_SELFCHECK_PROVIDER")
    llm_retry_provider: str = Field(default="dashscope", alias="LLM_RETRY_PROVIDER")
    llm_vision_provider: str = Field(default="dashscope", alias="LLM_VISION_PROVIDER")
    dashscope_model: str = Field(default="qwen-flash", alias="DASHSCOPE_MODEL")
    dashscope_rewrite_model: str = Field(
        default="qwen-flash",
        alias="DASHSCOPE_REWRITE_MODEL",
    )
    dashscope_planner_model: str = Field(
        default="qwen-flash",
        alias="DASHSCOPE_PLANNER_MODEL",
    )
    dashscope_reply_model: str = Field(default="qwen-plus", alias="DASHSCOPE_REPLY_MODEL")
    dashscope_selfcheck_model: str = Field(
        default="qwen-flash",
        alias="DASHSCOPE_SELFCHECK_MODEL",
    )
    dashscope_retry_model: str = Field(
        default="qwen-flash",
        alias="DASHSCOPE_RETRY_MODEL",
    )
    dashscope_vision_model: str = Field(default="qwen3.5-omni-plus", alias="DASHSCOPE_VISION_MODEL")

    def llm_provider_for(self, stage: str, *, retry: bool = False) -> str:
        if retry and self.llm_retry_provider.strip():
            return self.llm_retry_provider.strip().lower()
        stage_providers = {
            "rewrite": self.llm_rewrite_provider,
            "planner": self.llm_planner_provider,
            "reply": self.llm_reply_provider,
            "selfcheck": self.llm_selfcheck_provider,
            "retry": self.llm_retry_provider,
            "vision": self.llm_vision_provider,
        }
        provider = stage_providers.get(stage, "dashscope")
        return provider.strip().lower() or "dashscope"

    def llm_model_for(self, stage: str, *, retry: bool = False) -> str:
        provider = self.llm_provider_for(stage, retry=retry)
        if provider == "deepseek":
            if retry and self.deepseek_retry_model.strip():
                return self.deepseek_retry_model.strip()
            stage_models = {
                "rewrite": self.deepseek_rewrite_model,
                "planner": self.deepseek_planner_model,
                "reply": self.deepseek_reply_model,
                "selfcheck": self.deepseek_selfcheck_model,
                "retry": self.deepseek_retry_model,
            }
            model = stage_models.get(stage, "")
            return model.strip() or self.deepseek_model.strip()
        if retry and self.dashscope_retry_model.strip():
            return self.dashscope_retry_model.strip()
        stage_models = {
            "rewrite": self.dashscope_rewrite_model,
            "planner": self.dashscope_planner_model,
            "reply": self.dashscope_reply_model,
            "selfcheck": self.dashscope_selfcheck_model,
            "retry": self.dashscope_retry_model,
            "vision": self.dashscope_vision_model,
        }
        model = stage_models.get(stage, "")
        return model.strip() or self.dashscope_model.strip()

    def llm_api_key_for(self, provider: str) -> str:
        if provider.strip().lower() == "deepseek":
            return self.deepseek_api_key
        return self.dashscope_api_key

    def llm_base_url_for(self, provider: str) -> str:
        if provider.strip().lower() == "deepseek":
            return self.deepseek_base_url
        return self.dashscope_base_url

    def dashscope_model_for(self, stage: str, *, retry: bool = False) -> str:
        return self.llm_model_for(stage, retry=retry)

    inventory_source: str = "local_image"
    kdocs_public_url: str = ""
    inventory_cache_path: Path = Path("data/inventory_cache.csv")
    inventory_cache_meta_path: Path = Field(
        default=Path("data/inventory_cache_meta.json"),
        alias="INVENTORY_CACHE_META_PATH",
    )
    inventory_cache_max_age_seconds: int = Field(
        default=300,
        alias="INVENTORY_CACHE_MAX_AGE_SECONDS",
    )
    inventory_image_cache_path: Path = Path("data/inventory_image_cache.md")
    inventory_refresh_seconds: int = 300
    rewrite_inventory_index_path: Path = Field(
        default=Path("data/rewrite_inventory_index.json"),
        alias="REWRITE_INVENTORY_INDEX_PATH",
    )
    inventory_snapshot_mode: str = Field(
        default="disabled",
        alias="INVENTORY_SNAPSHOT_MODE",
    )
    inventory_snapshot_shadow_root: Path = Field(
        default=Path("data/inventory_snapshots_shadow"),
        alias="INVENTORY_SNAPSHOT_SHADOW_ROOT",
    )
    inventory_snapshot_shadow_stale_seconds: int = Field(
        default=24 * 60 * 60,
        alias="INVENTORY_SNAPSHOT_SHADOW_STALE_SECONDS",
    )
    inventory_snapshot_shadow_required_passes: int = Field(
        default=3,
        alias="INVENTORY_SNAPSHOT_SHADOW_REQUIRED_PASSES",
    )
    inventory_snapshot_shadow_timeout_seconds: float = Field(
        default=10.0,
        alias="INVENTORY_SNAPSHOT_SHADOW_TIMEOUT_SECONDS",
    )
    inventory_snapshot_shadow_report_retention: int = Field(
        default=30,
        alias="INVENTORY_SNAPSHOT_SHADOW_REPORT_RETENTION",
    )
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
    feishu_inventory_drive_folder_token: str = Field(
        default="",
        alias="FEISHU_INVENTORY_DRIVE_FOLDER_TOKEN",
    )
    feishu_sync_media_on_startup: bool = Field(
        default=False,
        alias="FEISHU_SYNC_MEDIA_ON_STARTUP",
    )
    feishu_media_sync_interval_seconds: int = Field(
        default=0,
        alias="FEISHU_MEDIA_SYNC_INTERVAL_SECONDS",
    )
    feishu_media_sync_min_seconds: int = Field(
        default=60,
        alias="FEISHU_MEDIA_SYNC_MIN_SECONDS",
    )
    feishu_media_sync_state_path: Path = Field(
        default=Path("data/feishu_media_sync_state.json"),
        alias="FEISHU_MEDIA_SYNC_STATE_PATH",
    )
    feishu_event_verify_token: str = Field(
        default="",
        alias="FEISHU_EVENT_VERIFY_TOKEN",
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
    feishu_leads_app_token: str = Field(default="", alias="FEISHU_LEADS_APP_TOKEN")
    feishu_leads_table_id: str = Field(default="", alias="FEISHU_LEADS_TABLE_ID")
    feishu_intermediary_summary_app_token: str = Field(
        default="",
        alias="FEISHU_INTERMEDIARY_SUMMARY_APP_TOKEN",
    )
    feishu_intermediary_summary_table_id: str = Field(
        default="",
        alias="FEISHU_INTERMEDIARY_SUMMARY_TABLE_ID",
    )
    feishu_kf_issue_app_token: str = Field(
        default="",
        alias="FEISHU_KF_ISSUE_APP_TOKEN",
    )
    feishu_kf_issue_table_id: str = Field(
        default="",
        alias="FEISHU_KF_ISSUE_TABLE_ID",
    )
    feishu_kf_issue_notify_webhook: str = Field(
        default="",
        alias="FEISHU_KF_ISSUE_NOTIFY_WEBHOOK",
    )
    feishu_region_sync_sources: str = Field(
        default="",
        alias="FEISHU_REGION_SYNC_SOURCES",
    )
    feishu_region_sync_target_spreadsheet_token: str = Field(
        default="H7f8sxOrUhYCK8tev29cwSimnsl",
        alias="FEISHU_REGION_SYNC_TARGET_SPREADSHEET_TOKEN",
    )
    feishu_region_sync_target_sheet_id: str = Field(
        default="",
        alias="FEISHU_REGION_SYNC_TARGET_SHEET_ID",
    )
    feishu_region_sync_target_drive_folder_token: str = Field(
        default="QJBOflSEklBFgwdTSeucBUj6nmh",
        alias="FEISHU_REGION_SYNC_TARGET_DRIVE_FOLDER_TOKEN",
    )
    feishu_region_sync_state_path: Path = Field(
        default=Path("data/feishu_region_sync_state.json"),
        alias="FEISHU_REGION_SYNC_STATE_PATH",
    )
    inventory_image_sync_state_path: Path = Field(
        default=Path("data/inventory_image_sync_state.json"),
        alias="INVENTORY_IMAGE_SYNC_STATE_PATH",
    )
    lead_event_log_path: Path = Field(
        default=Path("data/broker_leads.jsonl"),
        alias="LEAD_EVENT_LOG_PATH",
    )

    media_root: Path = Path("media/rooms")

    require_inventory_grounding: bool = True
    default_fallback_reply: str = (
        "\u6211\u5148\u5e2e\u60a8\u786e\u8ba4\u4e00\u4e0b\u6700\u65b0\u623f\u6001"
        "\uff0c\u7a0d\u540e\u7ed9\u60a8\u51c6\u786e\u56de\u590d\u3002"
    )


settings = Settings()
