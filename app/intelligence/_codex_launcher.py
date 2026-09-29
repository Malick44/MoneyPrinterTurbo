"""Launch the SDK's pinned app-server with a subscription-only environment.

CodexConfig.env is an overlay in openai-codex 0.147.0. This separate process is
necessary: removing a key from that overlay does not remove the inherited key.
No credentials are copied or written and the MPT process environment is intact.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path


def subscription_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in tuple(env):
        name = key.upper()
        if (
            name.startswith(("OPENAI_", "AZURE_OPENAI_", "ANTHROPIC_", "OPENROUTER_"))
            or name == "AWS_BEARER_TOKEN_BEDROCK"
            or (
                name.startswith("CODEX_")
                and name not in {"CODEX_HOME", "CODEX_CA_CERTIFICATE"}
            )
            or name.startswith("CLAUDE_CODE_USE_")
        ):
            env.pop(key)
    return env


def main() -> None:
    from codex_cli_bin import bundled_codex_path

    override = os.environ.get("MPT_CODEX_BIN", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise SystemExit(
                "MPT_CODEX_BIN must point to an existing absolute Codex executable."
            )
        binary = str(path.resolve())
    else:
        binary = str(bundled_codex_path())
    os.execve(binary, [binary, *sys.argv[1:]], subscription_environment())


if __name__ == "__main__":
    main()
