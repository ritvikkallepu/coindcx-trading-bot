import shutil
import zipfile
import os
from pathlib import Path

def package_clean():
    zip_name = "coindcx_trading_bot_clean.zip"
    exclude_dirs = {".git", ".venv", ".pytest_cache", "__pycache__", "logs", "outputs", "research/backtests", "data/repair_backups"}
    exclude_files = {".env", "paper_state.db", "paper_trades.csv", "data/paper_state.json", "data/paper_intrabar_audit.csv", zip_name}

    root = Path(".")
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in root.rglob("*"):
            if any(part in exclude_dirs for part in file_path.parts):
                continue
            if file_path.is_file():
                if file_path.name in exclude_files:
                    continue
                if file_path.suffix in {".pyc", ".pyo"}:
                    continue
                zipf.write(file_path, file_path)
    
    print(f"Created clean package: {zip_name}")

if __name__ == "__main__":
    package_clean()
