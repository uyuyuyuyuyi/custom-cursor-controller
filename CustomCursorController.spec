# -*- mode: python ; coding: utf-8 -*-
# Web GUI 版打包配置: pywebview + Vue 前端 + Python 后端
# 双击 CustomCursorController.exe 进入桌面窗口

a = Analysis(
    ['custom_cursor_gui.pyw'],
    pathex=[],
    binaries=[],
    datas=[
        ('webui/dist', 'webui'),
    ],
    hiddenimports=[
        'gui_server',
        'cursor_manager',
        'pointer_analyzer',
        'ani_builder',
        'webview',
        'webview.platforms.edgechromium',
        'clr_loader',
        'pythonnet',
        'pystray._win32',
    ],
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
    name='CustomCursorController',
    icon='assets/app.ico',
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
