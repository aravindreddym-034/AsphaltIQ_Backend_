"""Run from any directory: python start.py."""
import os
from pathlib import Path
import uvicorn
from main import create_app

if __name__ == "__main__":
    app = create_app()
    directory = Path(os.environ.get("ASPHALTIQ_DATA_DIR", Path(__file__).parent / "data"))
    print("\nAsphaltIQ: open http://127.0.0.1:8000 in your browser.")
    if not os.environ.get("ASPHALTIQ_TEAM_KEY"):
        print("Your team access key is in:", directory / "team.key")
        print("Copy its contents into the Team access key box on the page.")
    else:
        print("Use the team access key you configured in ASPHALTIQ_TEAM_KEY.")
    print("Keep this terminal open. Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
