from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
DRAFT_SCHEMA_VERSION = "survey-template-draft/v1"
LIKERT_FALLBACK_OPTIONS = [
    "Strongly disagree",
    "Disagree",
    "Neutral",
    "Agree",
    "Strongly agree",
]


@dataclass
class OptionScore:
    value: str
    box: tuple[int, int, int, int]
    score: float
    dark_ratio: float


def load_template(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "questions" not in data or not isinstance(data["questions"], list):
        raise SystemExit("template must contain questions[]")
    for q in data["questions"]:
        if "id" not in q or "options" not in q:
            raise SystemExit("each question needs id and options")
    return data


def _parse_option_labels(text: str) -> list[str]:
    option_lines = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if re.match(r"(?i)^(options?|scale|likert)\s*[:=]", normalized):
            option_lines.append(re.sub(r"^[^:=]+[:=]\s*", "", normalized, count=1))
        elif re.search(r"\b1\s*=", normalized) and re.search(r"\b5\s*=", normalized):
            option_lines.append(normalized)
    for line in option_lines:
        pairs = re.findall(r"(?:^|[|,;\s])(?:[1-9]|10)\s*[=:.)-]\s*([^|,;]+?)(?=\s*(?:[|,;]|\b(?:[1-9]|10)\s*[=:.)-])|$)", line)
        labels = [p.strip() for p in pairs if p.strip()]
        if len(labels) >= 2:
            return labels
        labels = [p.strip() for p in re.split(r"\s*[|;]\s*", line) if p.strip()]
        if len(labels) >= 2:
            return labels
    return LIKERT_FALLBACK_OPTIONS.copy()


def _parse_question_lines(text: str) -> list[str]:
    questions = []
    question_re = re.compile(r"^\s*(?:[-*]\s*)?(?:Q\s*)?(\d{1,3})[\).:-]\s+(.+?)\s*$", re.IGNORECASE)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or re.match(r"(?i)^(#|options?|scale|likert)\b", stripped):
            continue
        match = question_re.match(stripped)
        if match:
            question = match.group(2).strip()
            if question and not re.match(r"(?i)^(strongly|disagree|agree|neutral)\b", question):
                questions.append(question)
    return questions


def validate_template_draft(data: dict[str, Any]) -> None:
    errors = []
    if data.get("schema_version") != DRAFT_SCHEMA_VERSION:
        errors.append(f"schema_version must be {DRAFT_SCHEMA_VERSION}")
    if not isinstance(data.get("source"), dict):
        errors.append("source must be an object")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        errors.append("questions must be a non-empty array")
        questions = []
    for idx, question in enumerate(questions):
        prefix = f"questions[{idx}]"
        if not isinstance(question, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in ("id", "text", "type"):
            if not isinstance(question.get(key), str) or not question.get(key, "").strip():
                errors.append(f"{prefix}.{key} must be a non-empty string")
        options = question.get("options")
        if not isinstance(options, list) or len(options) < 2 or not all(isinstance(v, str) and v.strip() for v in options):
            errors.append(f"{prefix}.options must be an array of at least two labels")
        if question.get("page") is not None and not isinstance(question.get("page"), int):
            errors.append(f"{prefix}.page must be an integer or null")
        bbox = question.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, dict) or not all(k in bbox for k in ("x", "y", "width", "height")):
                errors.append(f"{prefix}.bbox must be null or an object with x/y/width/height")
            elif not all(isinstance(bbox[k], (int, float)) for k in ("x", "y", "width", "height")):
                errors.append(f"{prefix}.bbox values must be numeric")
        confidence = question.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{prefix}.confidence must be a number from 0 to 1")
    if errors:
        raise ValueError("; ".join(errors))


def extract_template_draft_offline(input_path: Path, sidecar_path: Path | None = None) -> dict[str, Any]:
    text_path = sidecar_path or input_path
    if text_path.suffix.lower() not in {".txt", ".md", ".ocr"}:
        raise SystemExit("offline template extraction needs --sidecar pointing to a .txt/.md/.ocr text dump")
    if not text_path.exists():
        raise SystemExit(f"sidecar text not found: {text_path}")
    text = text_path.read_text(encoding="utf-8")
    labels = _parse_option_labels(text)
    questions = _parse_question_lines(text)
    draft = {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "source": {
            "provider": "offline-text",
            "input": str(input_path),
            "sidecar": str(text_path),
        },
        "questions": [
            {
                "id": f"Q{idx}",
                "text": question,
                "type": "likert",
                "options": labels,
                "page": None,
                "bbox": None,
                "confidence": 0.85,
            }
            for idx, question in enumerate(questions, start=1)
        ],
    }
    validate_template_draft(draft)
    return draft


def build_openai_vision_prompt_package(input_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "openai-vision-template-extraction-prompt/v1",
        "provider_role": "AI/Vision drafts question text, Likert labels, and optional rough anchors from a blank form only.",
        "source": {"input": str(input_path)},
        "instructions": [
            "Read the blank survey form image/PDF and return only JSON.",
            f"Use schema_version {DRAFT_SCHEMA_VERSION} for the response.",
            "For each recognized question include id, text, type, options, page, bbox, and confidence.",
            "Use type='likert' when a Likert scale is likely; set bbox=null if unsure.",
            "Do not process completed response forms; completed-response OMR stays local-first.",
        ],
    }


def iter_input_images(input_path: Path, work_dir: Path) -> Iterable[Path]:
    if input_path.is_file():
        paths = [input_path]
    else:
        paths = sorted(p for p in input_path.iterdir() if p.is_file())
    for p in paths:
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:
            yield p
        elif ext in PDF_EXTS:
            if not shutil.which("pdftoppm"):
                raise SystemExit(f"PDF input needs poppler 'pdftoppm' installed: {p}")
            prefix = work_dir / p.stem
            subprocess.run(["pdftoppm", "-png", "-r", "200", str(p), str(prefix)], check=True)
            for rendered in sorted(work_dir.glob(p.stem + "-*.png")):
                yield rendered


def score_box(gray: Image.Image, box: list[int] | tuple[int, int, int, int], threshold: int = 180) -> tuple[float, float]:
    x, y, w, h = [int(v) for v in box]
    # Avoid border lines and printed labels by focusing on inner pixels.
    pad_x = max(2, round(w * 0.12))
    pad_y = max(2, round(h * 0.12))
    x1, y1, x2, y2 = x + pad_x, y + pad_y, x + w - pad_x, y + h - pad_y
    pix = gray.load()
    dark = 0
    very_dark = 0
    total = 0
    # Score central/diagonal ink extra; helps with check marks as well as filled bubbles.
    diag_bonus = 0
    for yy in range(max(0, y1), min(gray.height, y2)):
        for xx in range(max(0, x1), min(gray.width, x2)):
            v = pix[xx, yy]
            if v < threshold:
                dark += 1
            if v < 110:
                very_dark += 1
            rx = (xx - x1) / max(1, x2 - x1)
            ry = (yy - y1) / max(1, y2 - y1)
            if abs((ry - rx) - 0.08) < 0.16 and v < threshold:
                diag_bonus += 1
            total += 1
    score = dark + 0.8 * very_dark + 0.35 * diag_bonus
    return score, (dark / total if total else 0.0)


def read_image(image_path: Path, template: dict[str, Any], output_debug: Path | None = None) -> dict[str, Any]:
    image = Image.open(image_path).convert("RGB")
    gray = image.convert("L")
    draw = ImageDraw.Draw(image)
    rows = []
    min_conf = float(template.get("min_confidence", 0.12))
    no_mark_ratio = float(template.get("no_mark_dark_ratio", 0.015))
    for q in template["questions"]:
        scores = []
        for value, box in q["options"].items():
            score, dark_ratio = score_box(gray, box)
            scores.append(OptionScore(value=str(value), box=tuple(map(int, box)), score=score, dark_ratio=dark_ratio))
        scores.sort(key=lambda s: s.score, reverse=True)
        best = scores[0]
        second = scores[1] if len(scores) > 1 else OptionScore("", (0, 0, 0, 0), 0, 0)
        confidence = max(0.0, (best.score - second.score) / (best.score + 1.0))
        needs_review = confidence < min_conf or best.dark_ratio < no_mark_ratio
        reason = ""
        if best.dark_ratio < no_mark_ratio:
            reason = "no_mark_detected"
        elif confidence < min_conf:
            reason = "low_confidence"
        rows.append({
            "question_id": q["id"],
            "question_text": q.get("text", ""),
            "selected": best.value,
            "confidence": round(confidence, 4),
            "needs_review": needs_review,
            "review_reason": reason,
            "scores": {s.value: round(s.score, 2) for s in sorted(scores, key=lambda x: x.value)},
        })
        for s in scores:
            x, y, w, h = s.box
            color = "red" if s.value == best.value else "#33aa33"
            width = 4 if s.value == best.value else 2
            draw.rectangle([x, y, x + w, y + h], outline=color, width=width)
        x, y, w, h = best.box
        draw.text((x, max(0, y - 16)), f"{q['id']}={best.value} c={confidence:.2f}", fill="red")
    debug_path = None
    if output_debug:
        output_debug.mkdir(parents=True, exist_ok=True)
        debug_path = output_debug / f"{image_path.stem}_overlay.jpg"
        image.save(debug_path, quality=90)
    return {"file": image_path.name, "debug_overlay": str(debug_path) if debug_path else "", "answers": rows}


def write_results(results: list[dict[str, Any]], template: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    answer_key = template.get("answer_key", {}) or {}
    with (output_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["file", "question_id", "question_text", "selected", "confidence", "needs_review", "review_reason"]
        if answer_key:
            fieldnames += ["expected", "is_correct"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for item in results:
            for ans in item["answers"]:
                row = {k: ans.get(k, "") for k in fieldnames if k not in {"file", "expected", "is_correct"}}
                row["file"] = item["file"]
                if answer_key:
                    expected = str(answer_key.get(ans["question_id"], ""))
                    row["expected"] = expected
                    row["is_correct"] = (str(ans["selected"]) == expected) if expected else ""
                writer.writerow(row)
    total = sum(len(item["answers"]) for item in results)
    review = sum(1 for item in results for ans in item["answers"] if ans["needs_review"])
    correct = compared = 0
    if answer_key:
        for item in results:
            for ans in item["answers"]:
                expected = str(answer_key.get(ans["question_id"], ""))
                if expected:
                    compared += 1
                    correct += int(str(ans["selected"]) == expected)
    report = [
        "# Survey OMR report",
        "",
        f"- files_processed: {len(results)}",
        f"- answers_read: {total}",
        f"- needs_review: {review}",
    ]
    if compared:
        report += [f"- compared_to_answer_key: {compared}", f"- correct: {correct}", f"- accuracy: {correct / compared:.2%}"]
    (output_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_sample(root: Path) -> None:
    sample_dir = root / "samples" / "synthetic"
    sample_dir.mkdir(parents=True, exist_ok=True)
    width, height = 1000, 1400
    questions = []
    x0, y0 = 520, 220
    box = 42
    gap_x = 72
    gap_y = 82
    for q in range(1, 11):
        y = y0 + (q - 1) * gap_y
        opts = {str(v): [x0 + (v - 1) * gap_x, y - 20, box, box] for v in range(1, 6)}
        questions.append({"id": f"Q{q}", "text": f"Synthetic survey question {q}", "options": opts})
    template = {
        "survey_id": "synthetic_likert_demo",
        "page": {"width": width, "height": height},
        "min_confidence": 0.12,
        "no_mark_dark_ratio": 0.015,
        "questions": questions,
        "answer_key": {f"Q{q}": str(((q + 1) % 5) + 1) for q in range(1, 11)},
    }
    (root / "templates").mkdir(exist_ok=True)
    (root / "templates" / "synthetic_likert.json").write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    selections = {f"Q{q}": str(((q + 1) % 5) + 1) for q in range(1, 11)}
    for name, marked in [("blank_form.png", {}), ("answer_001.png", selections)]:
        im = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(im)
        draw.text((80, 80), "Synthetic Likert Survey (public dummy data)", fill="black")
        draw.text((80, 130), "1=Strongly disagree  5=Strongly agree", fill="black")
        for q in questions:
            idx = int(q["id"][1:])
            draw.text((80, y0 + (idx - 1) * gap_y - 8), f"{q['id']}. {q['text']}", fill="black")
            for v, boxv in q["options"].items():
                x, y, w, h = boxv
                draw.rectangle([x, y, x + w, y + h], outline="black", width=2)
                draw.text((x + 14, y + 10), v, fill="gray")
                if marked.get(q["id"]) == v:
                    draw.line([x + 7, y + 22, x + 17, y + 34, x + 36, y + 8], fill="black", width=6)
        im.save(sample_dir / name)
    sidecar_lines = [
        "# Synthetic Likert Survey OCR Sidecar",
        "",
        "Options: 1=Strongly disagree | 2=Disagree | 3=Neutral | 4=Agree | 5=Strongly agree",
        "",
    ]
    sidecar_lines.extend(f"{q['id']}. {q['text']}" for q in questions)
    (sample_dir / "blank_form.ocr.txt").write_text("\n".join(sidecar_lines) + "\n", encoding="utf-8")
    (sample_dir / "expected.csv").write_text("question_id,selected\n" + "\n".join(f"{k},{v}" for k, v in selections.items()) + "\n", encoding="utf-8")


def cmd_run(args: argparse.Namespace) -> None:
    template = load_template(Path(args.template))
    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    with tempfile.TemporaryDirectory(prefix="survey-omr-") as tmp:
        results = [read_image(p, template, debug_dir) for p in iter_input_images(Path(args.input), Path(tmp))]
    write_results(results, template, output_dir)
    print(f"wrote {output_dir / 'results.csv'}")
    print(f"wrote {output_dir / 'report.md'}")
    print(f"wrote debug overlays under {debug_dir}")


def cmd_extract_template(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if args.provider == "offline":
        draft = extract_template_draft_offline(input_path, Path(args.sidecar) if args.sidecar else None)
    elif args.provider == "openai-vision":
        if not args.prompt_package:
            raise SystemExit("openai-vision provider is call-free in this harness; pass --prompt-package to write the request package")
        draft = build_openai_vision_prompt_package(input_path)
    else:
        raise SystemExit(f"unsupported provider: {args.provider}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output_path}")


def cmd_validate_template_draft(args: argparse.Namespace) -> None:
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        validate_template_draft(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"template draft validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("template draft valid")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local-first template-based paper survey OMR harness")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-sample", help="create public synthetic sample form/template")
    gen.add_argument("--root", default=".")
    run = sub.add_parser("run", help="read completed form images/PDFs using a template")
    run.add_argument("--template", required=True)
    run.add_argument("--input", required=True)
    run.add_argument("--output", required=True)
    extract = sub.add_parser("extract-template", help="draft a blank-form question template from sidecar/OCR text")
    extract.add_argument("--provider", choices=["offline", "openai-vision"], default="offline")
    extract.add_argument("--input", required=True, help="blank form image/PDF, or text file when provider=offline")
    extract.add_argument("--sidecar", help="offline .txt/.md/.ocr dump containing recognized question text")
    extract.add_argument("--output", required=True, help="path to write the template draft JSON")
    extract.add_argument("--prompt-package", action="store_true", help="write a call-free OpenAI Vision prompt package instead of calling an API")
    validate = sub.add_parser("validate-template-draft", help="validate a template draft JSON file")
    validate.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    if args.command == "generate-sample":
        generate_sample(Path(args.root))
        print("generated synthetic sample")
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "extract-template":
        cmd_extract_template(args)
    elif args.command == "validate-template-draft":
        cmd_validate_template_draft(args)


if __name__ == "__main__":
    main()
