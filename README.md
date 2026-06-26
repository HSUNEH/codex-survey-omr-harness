# Codex Survey OMR Harness

Local-first, Codex-friendly harness for reading fixed-format paper survey forms.

The harness is designed for this workflow:

1. Register each survey form as a `template.json` with question text and option boxes.
2. Put real completed forms under `private/` (gitignored).
3. Run local OMR to produce `results.csv`, `report.md`, JSON, and debug overlays.

It intentionally does **not** send real survey images to Codex/OpenAI or any external API.

## Why this shape?

A fully automatic "any form in, perfect answers out" recognizer is fragile. For workshop/MVP usage, the reliable shape is:

```text
new survey form -> create/swap template JSON -> read many responses of that same form
```

Question text can come from an editable source file or manual input. Mark reading is local template-based OMR.

## Quickstart

Requires Python 3.10+ and `uv` or `pip`.

```bash
git clone https://github.com/HSUNEH/codex-survey-omr-harness.git
cd codex-survey-omr-harness
uv run survey-omr generate-sample --root .
uv run survey-omr run   --template templates/synthetic_likert.json   --input samples/synthetic/answer_001.png   --output outputs/demo
```

Expected outputs:

```text
outputs/demo/results.csv
outputs/demo/report.md
outputs/demo/results.json
outputs/demo/debug/answer_001_overlay.jpg
```

## Template schema

```json
{
  "survey_id": "family_relation_v1",
  "page": { "width": 1654, "height": 2339 },
  "min_confidence": 0.12,
  "no_mark_dark_ratio": 0.015,
  "questions": [
    {
      "id": "Q1",
      "text": "Question text",
      "options": {
        "1": [520, 200, 42, 42],
        "2": [592, 200, 42, 42],
        "3": [664, 200, 42, 42],
        "4": [736, 200, 42, 42],
        "5": [808, 200, 42, 42]
      }
    }
  ],
  "answer_key": {
    "Q1": "4"
  }
}
```

Boxes are `[x, y, width, height]` in the normalized image coordinate system. `answer_key` is optional; when present, results include correctness columns and the report includes accuracy.

## Privacy model

Committed/public:

- source code
- template schema
- synthetic samples

Never committed:

- `private/` real forms and responses
- `outputs/` generated real results
- raw `.hwp`, `.pdf`, `.jpg`, `.png` files outside the synthetic sample folder

See `.gitignore`.

## PDF inputs

Image files work directly. PDF inputs require Poppler's `pdftoppm` command installed locally. On macOS:

```bash
brew install poppler
```

## Current limits

- v1 assumes the completed form is already aligned to the template coordinate system, or came from a consistent scan/render pipeline.
- Phone-photo perspective correction is intentionally left as the next slice.
- Handwritten/free-text OCR is out of scope.

## Development checks

```bash
uv run survey-omr generate-sample --root .
uv run survey-omr run --template templates/synthetic_likert.json --input samples/synthetic/answer_001.png --output outputs/demo
python -m compileall src
```
