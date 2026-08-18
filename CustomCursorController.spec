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
    # onnxruntime 的调试/转换子模块会静态引入 torch、scipy、pandas 等，
    # 程序只使用 onnxruntime.capi 做推理，这些包全都不需要；显式排除可
    # 避免一次打包把整个 ML 环境卷进来（实测会从 ~46MB 膨胀到 ~250MB）。
    excludes=[
        'onnxruntime.transformers',
        'onnxruntime.quantization',
        'onnxruntime.tools',
        'torch',
        'tensorflow',
        'tensorboard',
        'scipy',
        'pandas',
        'matplotlib',
        'sklearn',
        'sympy',
        'networkx',
        'pytest',
        'botocore',
        'boto3',
        'sqlalchemy',
        'fsspec',
        'rich',
        'IPython',
        'jupyter',
    ],
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
