"""Hermes plugin entrypoint for TencentDB Agent Memory Relay."""

from __future__ import annotations

from .hermes_plugin import runtime


def _handle_command(args) -> None:
    command = getattr(args, "tencent_relay_command", None)
    if command == "status":
        code, msg = runtime.status()
        print(msg)
        raise SystemExit(code)
    if command == "health":
        code, msg = runtime.health()
        print(msg)
        raise SystemExit(code)
    if command == "start":
        code, msg = runtime.start()
        print(msg)
        raise SystemExit(code)
    if command == "stop":
        code, msg = runtime.stop()
        print(msg)
        raise SystemExit(code)
    if command == "setup":
        code, msg = runtime.setup()
        print(msg)
        raise SystemExit(code)
    if command == "enable":
        code, msg = runtime.enable_service()
        print(msg)
        raise SystemExit(code)
    if command == "disable":
        code, msg = runtime.disable_service()
        print(msg)
        raise SystemExit(code)
    raise SystemExit("Usage: hermes tencent-relay <setup|status|health|start|stop|enable|disable>")


def _setup_argparse(subparser) -> None:
    subparsers = subparser.add_subparsers(dest="tencent_relay_command")
    subparsers.required = True
    for cmd in ["setup", "status", "health", "start", "stop", "enable", "disable"]:
        p = subparsers.add_parser(cmd)
        p.set_defaults(handler=_handle_command)


def register(ctx) -> None:
    """Register the ``hermes tencent-relay`` CLI command with Hermes."""
    ctx.register_cli_command(
        name="tencent-relay",
        help="Relay to a TencentDB Agent Memory hub",
        setup_fn=_setup_argparse,
        handler_fn=_handle_command,
        description=(
            "Local HTTP relay so Hermes can reach a remote "
            "TencentDB Agent Memory Memory Core. "
            "See: hermes tencent-relay setup"
        ),
    )
