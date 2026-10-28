"""Regenerate examples/normalized.json from classify-raw.json using the example config:

    PINTAKE_HOUSEHOLD=household.example.toml uv run python examples/make_normalized.py
"""
import json
from pathlib import Path

from pintake import normalize

HERE = Path(__file__).parent
TAX = {"tags": [{"name": "utilities"}, {"name": "financial"}],
       "correspondents": [{"name": "Springfield Water Department"}, {"name": "Initech Inc"}],
       "document_types": [{"name": "Utility Bill"}, {"name": "Statement"}]}
out = []
for item in json.loads((HERE / "classify-raw.json").read_text()):
    text = (HERE / "extracted" / item["file"].replace(".pdf", ".txt")).read_text()
    meta = normalize.normalize(item["model_output"], TAX, text=text)
    meta = normalize.apply_series(meta, item["file"])
    out.append({"file": item["file"], "normalized": meta,
                "custom_fields": normalize.derive_custom_fields(item["file"], meta)})
(HERE / "normalized.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out, indent=2))
