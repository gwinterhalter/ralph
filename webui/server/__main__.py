"""Run the control-panel API:  python -m webui.server [--port 8787]

Reads OL_SUPERVISOR_DB_URL (+ OL_SUPERVISOR_STATE_DIR for command writes). If
OL_SUPERVISOR_WEBUI_STATIC points at a built `webui/app/dist`, the API also serves the UI at /.
"""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webui.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    import uvicorn  # noqa: PLC0415 - lazy: only the live server needs it

    from webui.server.app import create_app  # noqa: PLC0415

    app = create_app(static_dir=os.environ.get("OL_SUPERVISOR_WEBUI_STATIC"))
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
