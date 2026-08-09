"""PyInstaller 和源码运行共用的程序入口。"""


if __name__ == "__main__":
    from feichuan_downloader.single_instance import ensure_single_instance

    guard = ensure_single_instance()
    if guard is None:
        raise SystemExit(0)

    from feichuan_downloader.gui import run

    raise SystemExit(run(guard))
