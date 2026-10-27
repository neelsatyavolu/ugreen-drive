from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Startup values from the environment.

    Everything an admin can change in the web UI (Settings) is overlaid from
    `/config/settings.json` by `runtime_settings.apply()`; env values are the
    defaults until an admin saves something different.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shown in the page title, sign-in screen and emails.
    drive_name: str = "Drive"
    # Public origin, e.g. https://drive.example.com (Cloudflare Tunnel). Empty: the LAN address.
    public_base_url: str = ""
    # Direct LAN origin (http://<NAS-IP>:<WEB_PORT>). Empty: detected at startup.
    # When reachable from the browser, the UI hands the session off here to skip the tunnel.
    lan_origin: str = ""
    lan_prefer: bool = True
    # Port nginx listens on inside the container (host network, so also on the NAS).
    web_port: int = 8790

    # Google sign-in (optional). Each Google email must be linked to a NAS user in Settings.
    google_client_id: str = ""
    google_client_secret: str = ""

    # Empty: generated on first start and kept in /config/session_secret.
    session_secret: str = ""
    # Email-code sign-in (optional, Resend). State survives image updates on the config mount.
    resend_api_key: str = ""
    resend_from_email: str = ""
    email_sign_in_db_path: str = "/config/email_sign_in.sqlite3"
    # Mac app / `ugd` sign-ins (hashed tokens).
    cli_tokens_db_path: str = "/config/cli_tokens.sqlite3"
    config_dir: str = "/config"

    session_cookie: str = "ugreen_drive_session"
    session_max_age: int = 60 * 60 * 24 * 5  # 5 days

    # Shared folder to open first; empty picks the first one the user can open.
    default_share: str = ""
    # Fallback root when no shared folder is active (tests / single-folder installs).
    drive_root: str = "/data/volume1"
    shares_volume: str = "/data/volume1"
    # Comma-separated volume roots inside the container; missing ones are skipped.
    shares_volumes: str = "/data/volume1,/data/volume2,/data/volume3,/data/volume4"
    extra_shares_dir: str = "/data/extra"
    # UGOS Samba share definitions (source of truth for share names + valid users).
    samba_shares_conf: str = "/host-etc/samba/smbshare.conf"

    # UGOS API on the NAS itself: checks NAS passwords and 2FA codes.
    ugos_api_url: str = "http://127.0.0.1:9999"
    # UGOS web interface link in the sidebar (admins). Empty: http://<NAS-IP>:9999.
    ugos_admin_path: str = ""
    ugos_admin_user: str = ""
    ugos_admin_password: str = ""
    personal_credential_path: str = "/config/personal_service.enc"
    personal_state_path: str = "/config/personal_folders.sqlite3"

    host: str = "0.0.0.0"
    port: int = 8787


@lru_cache
def get_settings() -> Settings:
    return Settings()


SESSION_SECRET_MIN_LEN = 32
_PLACEHOLDER_SECRETS = ("generate-a-long-random-string",)


def require_strong_session_secret(secret: str) -> None:
    """Raise unless SESSION_SECRET is set to a real random value.

    It signs session cookies, OAuth state, LAN handoffs and file links, and
    encrypts stored UGOS sessions, so a guessable value is a full compromise.
    """
    value = (secret or "").strip()
    if (
        len(value) < SESSION_SECRET_MIN_LEN
        or value.lower().startswith("change-me")
        or value.lower() in _PLACEHOLDER_SECRETS
    ):
        raise RuntimeError(
            f"SESSION_SECRET must be a random string of at least {SESSION_SECRET_MIN_LEN} "
            "characters (e.g. `openssl rand -hex 32`); refusing to start with a "
            "missing, placeholder, or short value."
        )
