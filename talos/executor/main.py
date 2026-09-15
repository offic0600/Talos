"""Entry point for ``python -m talos.executor.main`` (systemd ExecStart)."""

from talos.executor.loop import run_executor


def main() -> None:
    """Console-script entry point (pyproject.toml ``[project.scripts]``)."""
    run_executor()


if __name__ == "__main__":
    main()
