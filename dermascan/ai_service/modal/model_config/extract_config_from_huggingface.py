"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly."""

import os
import json
from dotenv import load_dotenv
from transformers import AutoConfig

load_dotenv()

def create_config_file():
    token = os.environ.get("HF_TOKEN")
    config = AutoConfig.from_pretrained("google/medgemma-1.5-4b-it", token=token)
    with open("model_config.json", "w") as f:
        json.dump(config.to_dict(), f, indent=2)


if __name__ == "__main__":
    create_config_file()

