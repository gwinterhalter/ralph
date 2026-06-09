"""Run the control-panel API + UI:  python -m webui.server [--port 8787]

Reads OL_SUPERVISOR_DB_URL (+ OL_SUPERVISOR_STATE_DIR for command writes). Serves the built UI at /
automatically when `webui/app/dist` exists (build it with `cd webui/app && npm run build`);
OL_SUPERVISOR_WEBUI_STATIC overrides the location. With no build present it runs API-only and `/`
returns a short how-to instead of a bare 404.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webui.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    import uvicorn  # noqa: PLC0415 - lazy: only the live server needs it

    from webui.server.app import _default_static_dir, create_app  # noqa: PLC0415

    static = _default_static_dir()
    app = create_app(static_dir=static)
    print(f"webui: UI {'served from ' + static if static else 'NOT built (API-only; see / for help)'}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
