"""One bounded SDK operation, communicating only safe JSON through stdio."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Invoked by absolute path so launching from API, CLI, or Streamlit is identical.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.intelligence.runtime import CodexRuntime, IntelligenceError  # noqa: E402


def main() -> int:
    try:
        request = json.load(sys.stdin)
        runtime = CodexRuntime(
            model=request.get("model"),
            reasoning_effort=request.get("reasoning_effort", "medium"),
            working_directory=request.get("working_directory"),
        )
        if request["operation"] == "status":
            result = runtime._auth_status_sdk()
        elif request["operation"] == "generate":
            result = runtime._run_sdk(
                request["prompt"], request.get("schema"), request.get("images", ())
            )
        else:
            raise IntelligenceError("codex_response", "Unknown Codex operation.")
        print(json.dumps({"result": result}))
        return 0
    except IntelligenceError as exc:
        print(json.dumps({"stage": exc.stage, "error": exc.message}))
    except Exception:
        print(
            json.dumps(
                {
                    "stage": "codex_response",
                    "error": "The Codex SDK runtime failed. Run `uv sync` and retry.",
                }
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
