# Synthetic examples

Fictional documents (no real people, addresses or accounts) run through the
stages against `household.example.toml`:

| file | stage |
|---|---|
| `extracted/*.txt` | `extract` output (pdftotext / OCR text stored in `state.db`) |
| `classify-raw.json` | `classify` raw model JSON (schema in `pintake/classify.py`) |
| `normalized.json` | after `normalize.py` + series rules + derived custom fields |

Things to notice in `normalized.json`:

- `sam` dropped: the person guard requires the document to name them.
- `taxes` dropped: guarded tag, and a water bill is not a tax document.
- `maple` -> `prop:12Maple` and derived from `properties`, not the free tag list.
- `Springfield Water Dept.` snapped to the existing `Springfield Water Department`;
  `pay stub` -> document type `Statement`; `initech inc` banned as a tag.
- `Entity` and `Unit` custom fields derived from the property map and the title.
- the pay stub has an empty `properties` list even though it shows the
  residence's mailing address.

Regenerate: `PINTAKE_HOUSEHOLD=household.example.toml uv run python examples/make_normalized.py`
