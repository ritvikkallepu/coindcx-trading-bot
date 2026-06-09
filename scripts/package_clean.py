import shutil
import zipfile
import os
from pathlib import Path

def package_clean():
    zip_name = "coindcx_trading_bot_clean.zip"
    exclude_dirs = {".git", ".venv", ".pytest_cache", "__pycache__", "logs", "outputs", "research/backtests"}
    exclude_files = {
        "paper_state.db", 
        "data/live_state.json",
    }
    
    # Also exclude by extension: .pyc, .pyo, .db, .sqlite
    exclude_extensions = {".pyc", ".pyo", ".db", ".sqlite"}

    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk("."):
            # Skip excluded dirs
            dirs[:] = [d for d in dirs if os.path.join(root, d).replace("\\", "/").lstrip("./") not in exclude_dirs]
            
            for file in files:
                file_path = Path(root) / file
                rel_path = file_path.as_posix().lstrip("./")
                
                # Exclude specific files
                if rel_path in exclude_files:
                    continue
                
                # Exclude by extension
                if file_path.suffix in exclude_extensions:
                    continue
                
                # Exclude audit files
                if "paper_intrabar_audit" in file and file.endswith(".csv"):
                    continue
                
                zipf.write(file_path, file_path)
    
    print(f"Created clean package: {zip_name}")

if __name__ == "__main__":
    package_clean()
