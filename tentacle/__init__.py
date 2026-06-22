"""TENTACLE 🐙: a free, self-learning news/whale auto-trader.

The bot raises its capital like a child: it starts at $0 paper, earns its way up
a graduation ladder rung by rung, and only ever risks real money once it has
proven itself. Discipline over wins; the kill switch and the confidence gate ARE
the strategy.

See README.md for the full design. This package is the single source of truth in
code form; when code and the design prompt disagree, the code is wrong, so fix it.
"""

__version__ = "0.1.0"


def load_env_file() -> None:
    """Load KEY=VALUE pairs from a repo-root .env into the environment.

    This runs at PACKAGE IMPORT time, on purpose: config.py reads env vars when its
    module-level constants and dataclass field defaults are evaluated (at import),
    so .env must be in os.environ before any submodule is imported. It never
    overrides a var already set in the real environment, and it is a no-op if
    TENTACLE_NO_DOTENV is set or the file is absent.
    """
    import os

    if os.environ.get("TENTACLE_NO_DOTENV"):
        return
    root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(root), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


# Load .env before any other tentacle submodule (especially config) is imported.
load_env_file()
