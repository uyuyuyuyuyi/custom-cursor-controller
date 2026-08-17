"""test_concurrent_save.py — CursorStore 并发写索引回归测试。"""
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui_server import CursorStore  # noqa: E402


def main():
    with tempfile.TemporaryDirectory(prefix="custom_cursor_race_") as root:
        store = CursorStore(root=root)
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    store.add_upload(
                        f"f{n}-{i}.png", b"x" * 100, b"png",
                        {"created": "", "size": [1, 1]},
                    )
            except Exception as e:  # noqa: BLE001
                errors.append(f"worker{n}: {e!r}")

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        if errors:
            print("\n".join(errors))
        with open(os.path.join(root, "index.json"), encoding="utf-8") as f:
            idx = json.load(f)  # 损坏的 JSON 会在此抛异常
        print(f"index entries: {len(idx['uploads'])} (expect 200), errors: {len(errors)}")

        assert not errors, errors
        assert len(idx["uploads"]) == 200
        # 磁盘文件与索引一致性抽查
        for e in idx["uploads"].values():
            assert os.path.isfile(os.path.join(root, e["filename"]))
        print("CONCURRENT_SAVE_OK")


if __name__ == "__main__":
    main()
