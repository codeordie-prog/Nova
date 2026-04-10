import socket
from pathlib import Path
import os
from dotenv import load_dotenv

_ROOT_PATH = Path(__file__).resolve().parents[4]

load_dotenv(_ROOT_PATH / ".env")

print(os.environ.get("REDIS_PORT"))
