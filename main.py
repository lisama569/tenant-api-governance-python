from pathlib import Path

import uvicorn

from app.config import Settings
from app.main import create_app

app = create_app(Settings(db_path=Path("governance.db")))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
