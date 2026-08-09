# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\飞船下载工具开发\\src\\main.py'],
    pathex=['D:\\飞船下载工具开发\\src'],
    binaries=[],
    datas=[],
    hiddenimports=['websocket', 'websocket._abnf', 'feichuan_downloader.douyin_enumerator', 'feichuan_downloader.douyin_session', 'feichuan_downloader.douyin_downloader', 'feichuan_downloader.youtube_enumerator', 'feichuan_downloader.youtube_downloader'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='飞船下载工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
