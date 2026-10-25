#!/usr/bin/env python3
"""Build the runtime-only ZIP. Only files listed here enter the archive."""
import hashlib
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_FOLDER = "Allen_heath_Dlive_v2.12_Full_Mirroring"
RUNTIME_FILES = (
    "allen_heath_dlive_full_mirroring.py", "ahnet_lab.py", "legacy_lab.py",
    "captured_rx.py", "surface_link.py", "layout_map.py", "safety_filter.py",
    "recovery_queue.py", "redundancy.py", "scene_sync.py", "index.html",
    "catalog.json", "capture_subscriptions.json", "global_tempo_subscription.json",
    "surface_subscriptions.json", "surface_control_subscriptions.json",
    "README_RU.md", "README_EN.md", "Allen_heath_Dlive_v2.12_Full_Mirroring.command",
    "Allen_heath_Dlive_v2.12_Full_Mirroring_Windows.bat",
)

def build(output_dir=None):
    output = pathlib.Path(output_dir or ROOT / "dist")
    output.mkdir(parents=True, exist_ok=True)
    path = output / (APP_FOLDER + "_0.6.6.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in RUNTIME_FILES:
            source = ROOT / name
            if not source.is_file():
                raise FileNotFoundError(source)
            info = zipfile.ZipInfo(APP_FOLDER + "/" + name)
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".command") else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(digest + "  " + path.name + "\n", encoding="utf-8")
    print(path)
    print("SHA256:", digest)
    return path

if __name__ == "__main__":
    build()
