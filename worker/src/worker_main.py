"""PyInstaller and source entry point for the headless Feichuan worker."""

from feichuan_downloader.worker import run


if __name__ == "__main__":
    raise SystemExit(run())
