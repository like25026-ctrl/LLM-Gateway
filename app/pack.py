import zipfile
import os

def pack_project():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    zip_path = os.path.join(root_dir, "agent-platform.zip")

    excludes = {"__pycache__", ".pyc", ".pyo", ".zip", "pack.py", ".git", "venv", ".env", ".db"}

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d not in excludes]
            for filename in filenames:
                if any(filename.endswith(ext) for ext in excludes):
                    continue
                filepath = os.path.join(dirpath, filename)
                arcname = os.path.relpath(filepath, root_dir)
                zf.write(filepath, arcname)

    print(f"打包完成: {zip_path}")

if __name__ == "__main__":
    pack_project()