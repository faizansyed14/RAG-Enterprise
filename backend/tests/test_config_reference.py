"""docs/CONFIGURATION.md is generated from the settings classes; it must never drift from the code."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_configuration_reference_is_current():
    out = subprocess.run(
        [sys.executable, "-m", "scripts.make_config_reference", "--check"],
        cwd=ROOT / "backend", capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_env_examples_only_name_real_settings():
    import re

    from app.core.config import Settings
    from app.scale.settings import ScaleSettings

    from app.scale.managed import ALL_ENV_NAMES

    known = ({n.upper() for n in Settings.model_fields} | {f"SCALE_{n.upper()}" for n in ScaleSettings.model_fields}
             | set(ALL_ENV_NAMES))                       # SHAREPOINT_* / ONEDRIVE_*: read by app/scale/managed.py
    # connector secrets are read by the NAME stored in a connection (client_secret_env), so they are not fields.
    # The rest are read by compose / scripts / third-party containers rather than by the Python settings
    compose_only = re.compile(
        r"^(PUBLIC_URL|STORAGE_PUBLIC_URL|SCALE_SOURCE_DIR|.*_CLIENT_SECRET|SMB_PASSWORD|TLS_CERT_DIR|POSTGRES_(USER|PASSWORD|DB)|MINIO_ROOT_.*|PGDATA_DIR|QDRANT_DATA_DIR|RAG_DATA_DIR|.*_PORT|.*_IMAGE|"
        r"NEXT_PUBLIC_.*|HF_.*|CADDY_.*|DOMAIN|ACME_.*|S3_BUCKET_.*|QDRANT_API_KEY|UVICORN_.*|WORKERS?|.*_REPLICAS|"
        r"TZ|COMPOSE_.*|FRONTEND_.*|BACKEND_.*|.*_CPUS|.*_MEM.*)$"
    )
    for name in (".env.dev.example", ".env.prod.example"):
        text = (ROOT / name).read_text()
        for m in re.finditer(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M):
            var = m.group(1)
            assert var in known or compose_only.match(var), f"{name}: {var} is not a setting the code reads"
