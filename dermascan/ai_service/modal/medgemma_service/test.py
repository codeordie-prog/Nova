from pathlib import Path

config_path = Path(__file__).resolve().parents[1] / "model_config" / "config.json"

print(config_path)