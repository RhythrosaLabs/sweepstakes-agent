"""Allow running as: python -m sweepstakes [ui|...]"""
import sys


def cli_entry():
    """Entry point for the `sweepstakes` console script."""
    if len(sys.argv) > 1 and sys.argv[1] == "ui":
        sys.argv.pop(1)
        from sweepstakes.ui import launch_ui

        launch_ui(port=7860)
    else:
        from sweepstakes.agent import main

        main()


if __name__ == "__main__":
    cli_entry()
