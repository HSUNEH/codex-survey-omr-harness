# Codex Survey OMR Harness

Local-first, Codex-friendly harness for reading fixed-format paper survey forms.

The harness is designed for this workflow:

1. Draft question text/options from a blank form with `survey-omr extract-template`.
2. Convert/review that draft into a coordinate-bearing OMR `template.json`.
3. Put real completed forms under `private/` (gitignored).
4. Run local OMR to produce `results.csv`, `report.md`, JSON, and debug overlays.

Completed-response OMR intentionally does **not** send real survey images to Codex/OpenAI or any external API.

## Why this shape?

A fully automatic "any form in, perfect answers out" recognizer is fragile. For workshop/MVP usage, the reliable shape is:

```text
new survey form -> draft/review template JSON -> read many responses of that same form
```

Question text can come from an editable source file, OCR-like sidecar, AI/Vision draft, or manual input. Mark reading is local template-based OMR.

## Quickstart

Requires Python 3.10+ and `uv` or `pip`.

```bash
git clone https://github.com/HSUNEH/codex-survey-omr-harness.git
cd codex-survey-omr-harness
uv run survey-omr generate-sample --root .
uv run survey-omr extract-template \
  --provider offline \
  --input samples/synthetic/blank_form.png \
  --sidecar samples/synthetic/blank_form.ocr.txt \
  --output outputs/template_draft.json
uv run survey-omr validate-template-draft --input outputs/template_draft.json
uv run survey-omr run \
  --template templates/synthetic_likert.json \
  --input samples/synthetic/answer_001.png \
  --output outputs/demo
```

Expected outputs:

```text
outputs/template_draft.json
outputs/demo/results.csv
outputs/demo/report.md
outputs/demo/results.json
outputs/demo/debug/answer_001_overlay.jpg
```

## Blank-form template draft extraction

`extract-template` produces a draft JSON with this shape:

```json
{
  "schema_version": "survey-template-draft/v1",
  "source": { "provider": "offline-text", "input": "samples/synthetic/blank_form.png" },
  "questions": [
    {
      "id": "Q1",
      "text": "Synthetic survey question 1",
      "type": "likert",
      "options": ["Strongly disagree", "Disagree", "Neutral", "Agree", "Strongly agree"],
      "page": null,
      "bbox": null,
      "confidence": 0.85
    }
  ]
}
```

Providers:

- `offline` (default): no secrets and no network. It reads a `.txt`, `.md`, or `.ocr` sidecar/OCR-like dump and extracts numbered questions plus likely Likert option labels. This is the CI/sample path.
- `openai-vision`: call-free prompt-package path for Codex/OpenAI Vision style extraction from a blank form. It writes the request instructions with `--prompt-package` but does not call an API. Use this only for blank forms/template drafting, never completed responses.

Examples:

```bash
uv run survey-omr extract-template \
  --provider offline \
  --input samples/synthetic/blank_form.png \
  --sidecar samples/synthetic/blank_form.ocr.txt \
  --output outputs/template_draft.json

uv run survey-omr validate-template-draft --input outputs/template_draft.json

uv run survey-omr extract-template \
  --provider openai-vision \
  --prompt-package \
  --input private/blank_forms/example.pdf \
  --output private/openai_vision_prompt_package.json
```

The draft is not a full OMR template yet: `bbox` is nullable, and the MVP does not attempt arbitrary coordinate detection from photos. Review the draft, add stable option boxes, then run local completed-response OMR with `survey-omr run`.

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
- AI/Vision prompt packages or extracted drafts from real forms unless they are sanitized and intentionally public

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
- Template draft extraction recognizes questions/options only; full arbitrary coordinate detection from blank-form photos is out of scope for this MVP.

## Development checks

```bash
uv run survey-omr generate-sample --root .
uv run survey-omr extract-template --provider offline --input samples/synthetic/blank_form.png --sidecar samples/synthetic/blank_form.ocr.txt --output outputs/template_draft.json
uv run survey-omr validate-template-draft --input outputs/template_draft.json
uv run survey-omr run --template templates/synthetic_likert.json --input samples/synthetic/answer_001.png --output outputs/demo
python -m compileall src
```
