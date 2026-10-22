from pathlib import Path
import json

PROFILE_PATH=Path(__file__).resolve().parents[1]/'examples'/'roadmap_profile.example.json'

def load_profile(path=PROFILE_PATH):
    with Path(path).open(encoding='utf-8') as handle: return json.load(handle)
