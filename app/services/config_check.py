from importlib import metadata

from app.config import settings
from app.services.inventory_images import inventory_image_glob_paths


def _installed_version(package_name: str) -> str:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return ""


def get_config_status() -> dict:
    fields = {
        "DASHSCOPE_API_KEY": settings.dashscope_api_key,
    }
    if settings.wecom_connection_mode == "long_connection":
        fields["WECOM_AIBOT_BOT_ID"] = settings.wecom_aibot_bot_id
        fields["WECOM_AIBOT_SECRET"] = settings.wecom_aibot_secret
    elif settings.wecom_connection_mode == "kf":
        fields["PUBLIC_BASE_URL"] = settings.public_base_url
        fields["WECOM_CORP_ID"] = settings.wecom_corp_id
        fields["WECOM_KF_SECRET"] = settings.wecom_kf_secret
        fields["WECOM_KF_TOKEN"] = settings.wecom_kf_token or settings.wecom_token
        fields["WECOM_KF_AES_KEY"] = settings.wecom_kf_aes_key or settings.wecom_aes_key
    else:
        fields["PUBLIC_BASE_URL"] = settings.public_base_url
        fields["WECOM_CORP_ID"] = settings.wecom_corp_id
        fields["WECOM_AGENT_ID"] = settings.wecom_agent_id
        fields["WECOM_SECRET"] = settings.wecom_secret
        fields["WECOM_TOKEN"] = settings.wecom_token
        fields["WECOM_AES_KEY"] = settings.wecom_aes_key
    if settings.inventory_source == "kdocs":
        fields["KDOCS_PUBLIC_URL"] = settings.kdocs_public_url
    if settings.inventory_source == "feishu_bitable":
        fields["FEISHU_APP_ID"] = settings.feishu_app_id
        fields["FEISHU_APP_SECRET"] = settings.feishu_app_secret
        fields["FEISHU_BITABLE_APP_TOKEN"] = settings.feishu_bitable_app_token
        fields["FEISHU_BITABLE_TABLE_ID"] = settings.feishu_bitable_table_id
    if settings.feishu_sync_media_on_startup or settings.feishu_drive_root_folder_token:
        fields["FEISHU_APP_ID"] = settings.feishu_app_id
        fields["FEISHU_APP_SECRET"] = settings.feishu_app_secret
        fields["FEISHU_DRIVE_ROOT_FOLDER_TOKEN"] = settings.feishu_drive_root_folder_token
    if settings.inventory_source == "local_image":
        fields["INVENTORY_IMAGE_PATH"] = str(settings.inventory_image_path)
    missing = [
        key
        for key, value in fields.items()
        if is_missing_or_placeholder(value)
    ]
    inventory_images = inventory_image_glob_paths()
    if not inventory_images and settings.inventory_image_path.exists():
        inventory_images = [settings.inventory_image_path]
    inventory_image_exists = bool(inventory_images)
    if settings.inventory_source == "local_image" and not inventory_image_exists:
        missing.append("INVENTORY_IMAGE_PATH_FILE")
    langgraph_installed = bool(_installed_version("langgraph"))
    langgraph_sqlite_installed = bool(_installed_version("langgraph-checkpoint-sqlite"))
    langgraph_errors: list[str] = []
    langgraph_required_for_production = str(settings.kf_dual_llm_mode).strip().lower() == "production"
    if langgraph_required_for_production and not settings.kf_langgraph_enabled:
        langgraph_errors.append("KF_LANGGRAPH_ENABLED_REQUIRED_FOR_PRODUCTION")
    if langgraph_required_for_production and not langgraph_installed:
        langgraph_errors.append("LANGGRAPH_PACKAGE_REQUIRED_FOR_PRODUCTION")
    missing.extend(langgraph_errors)
    return {
        "ok": not missing,
        "missing": missing,
        "callback_url": f"{settings.public_base_url.rstrip('/')}/wecom/callback",
        "kf_callback_url": f"{settings.public_base_url.rstrip('/')}/wecom/kf/callback",
        "connection_mode": settings.wecom_connection_mode,
        "inventory_source": settings.inventory_source,
        "inventory_image_exists": inventory_image_exists,
        "inventory_image_count": len(inventory_images),
        "langgraph": {
            "enabled": settings.kf_langgraph_enabled,
            "required_for_production": langgraph_required_for_production,
            "errors": langgraph_errors,
            "installed": langgraph_installed,
            "version": _installed_version("langgraph"),
            "sqlite_checkpoint_installed": langgraph_sqlite_installed,
            "checkpoint_path": str(settings.kf_langgraph_checkpoint_path),
            "smoke_thread_id": settings.kf_langgraph_smoke_thread_id,
        },
    }


def is_missing_or_placeholder(value: str) -> bool:
    text = str(value).strip()
    lowered = text.lower()
    if not text:
        return True
    placeholder_fragments = (
        "your_",
        "your-domain",
        "example",
        "xxxxxxxx",
        "placeholder",
    )
    return any(fragment in lowered for fragment in placeholder_fragments)
