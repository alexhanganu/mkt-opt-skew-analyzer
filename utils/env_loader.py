"""
utils/env_loader.py — Environment variable bootstrap

Loads API keys from a local .env file if present,
then validates that required keys are set before any run.

Priority order:
  1. OS environment variables (already exported in shell)
  2. .env file in project root (never committed to git)
  3. Fallback to empty string (triggers warning)
"""

import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).parent.parent / ".env"


def load_dotenv():
    """
    Parse a simple KEY=VALUE .env file and inject into os.environ.
    Ignores lines starting with # and blank lines.
    Does NOT override variables already set in the environment.
    """
    if not ENV_FILE.exists():
        return

    loaded = 0
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Only set if not already in environment (env vars take priority)
            if key and key not in os.environ:
                os.environ[key] = val
                loaded += 1

    if loaded:
        logger.debug(f"Loaded {loaded} variable(s) from {ENV_FILE}")


def validate_keys(require_anthropic: bool = True,
                  require_data: bool = False) -> bool:
    """
    Validate that required API keys are present.
    Prints clear guidance if keys are missing.
    Returns True if all required keys are present.
    """
    load_dotenv()

    ok = True
    issues = []

    # ── Anthropic ──────────────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if require_anthropic and not anthropic_key:
        issues.append(
            "ANTHROPIC_API_KEY is not set.\n"
            "  Set it in your shell:  export ANTHROPIC_API_KEY='sk-ant-...'\n"
            "  Or add to .env file:   ANTHROPIC_API_KEY=sk-ant-...\n"
            "  Without this key, Claude narrative analysis will be skipped.\n"
            "  Run with --no-claude to suppress this warning."
        )
        ok = False
    elif anthropic_key and not anthropic_key.startswith("sk-ant-"):
        issues.append(
            "ANTHROPIC_API_KEY looks malformed (should start with 'sk-ant-').\n"
            "  Check your key at console.anthropic.com"
        )
        ok = False

    # ── Data source ─────────────────────────────────────────────
    data_source = os.environ.get("DATA_SOURCE", "yfinance")

    if data_source == "tradier":
        tradier_key = os.environ.get("TRADIER_API_KEY", "")
        if not tradier_key:
            issues.append(
                "DATA_SOURCE=tradier but TRADIER_API_KEY is not set.\n"
                "  Get your key at: tradier.com → Account → API Access\n"
                "  Or switch to: DATA_SOURCE=yfinance in config.py"
            )
            ok = False if require_data else ok

    elif data_source == "polygon":
        polygon_key = os.environ.get("POLYGON_API_KEY", "")
        if not polygon_key:
            issues.append(
                "DATA_SOURCE=polygon but POLYGON_API_KEY is not set.\n"
                "  Get your key at: polygon.io → Dashboard → API Keys\n"
                "  Or switch to: DATA_SOURCE=yfinance in config.py"
            )
            ok = False if require_data else ok

    # ── Report ──────────────────────────────────────────────────
    if issues:
        print("\n" + "─" * 60)
        print("⚠  CONFIGURATION ISSUES:")
        print("─" * 60)
        for i, issue in enumerate(issues, 1):
            print(f"\n{i}. {issue}")
        print("\n" + "─" * 60 + "\n")

    return ok


def print_env_status():
    """Print a summary of all API key statuses."""
    load_dotenv()
    keys = {
        "ANTHROPIC_API_KEY":  os.environ.get("ANTHROPIC_API_KEY", ""),
        "TRADIER_API_KEY":    os.environ.get("TRADIER_API_KEY", ""),
        "POLYGON_API_KEY":    os.environ.get("POLYGON_API_KEY", ""),
    }
    print("\nAPI Key Status:")
    print("─" * 40)
    for k, v in keys.items():
        if v:
            masked = v[:8] + "..." + v[-4:] if len(v) > 12 else "***"
            print(f"  ✓  {k:<25} {masked}")
        else:
            print(f"  ✗  {k:<25} NOT SET")
    print()
