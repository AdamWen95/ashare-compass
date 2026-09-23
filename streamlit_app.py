"""Local, read-only M4 viewer. Start with the project's virtual environment."""

from pathlib import Path

from ashare_daily.viewer import render_app


if __name__ == "__main__":
    render_app(Path(__file__).resolve().parent / "outputs")
