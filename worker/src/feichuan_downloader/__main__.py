if __name__ == "__main__":
    from .single_instance import ensure_single_instance

    guard = ensure_single_instance()
    if guard is None:
        raise SystemExit(0)

    from .gui import run

    raise SystemExit(run(guard))
