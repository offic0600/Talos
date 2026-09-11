"""Entry point for ``python -m talos.executor.main`` (systemd ExecStart)."""

from talos.executor.loop import run_executor

if __name__ == "__main__":
    run_executor()
