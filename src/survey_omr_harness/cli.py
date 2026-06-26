from __future__ import annotations

import argparse
import csv
import json
import base64
import math
import os
import re
import shutil
import urllib.request
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}


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
    (sample_dir / "expected.csv").write_text("question_id,selected\n" + "\n".join(f"{k},{v}" for k, v in selections.items()) + "\n", encoding="utf-8")
    sample_text = "# Synthetic Likert Survey\n\n" + "\n".join(f"{q['id'][1:]}. {q['text']}" for q in questions) + "\n"
    (sample_dir / "blank_form_ocr.txt").write_text(sample_text, encoding="utf-8")


def normalize_options(values: list[str] | None = None) -> dict[str, None]:
    values = values or ["1", "2", "3", "4", "5"]
    return {str(v): None for v in values}


def parse_questions_from_text(text: str) -> list[dict[str, Any]]:
    """Extract numbered survey questions from OCR/markdown/plain text.

    This intentionally handles a conservative, reviewable subset: numbered lines
    like `1. question`, `Q1 question`, or markdown table rows. A Vision/Codex
    provider can produce this OCR-like text, while tests use the offline path.
    """
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= {"|", "-", ":", " "}:
            continue
        # Markdown row: | 1 | Question text | ... |
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0].isdigit() and not set(cells[1]) <= {"-", ":", " "}:
                qid = f"Q{int(cells[0])}"
                question = cells[1]
            else:
                continue
        else:
            match = re.match(r"^(?:Q\s*)?(\d{1,3})[\).\-\s]+(.+?)\s*$", line, flags=re.IGNORECASE)
            if not match:
                continue
            qid = f"Q{int(match.group(1))}"
            question = match.group(2).strip()
        # Strip trailing checkbox/scale artifacts from OCR-like dumps.
        question = re.sub(r"\s+(?:[1-5]\s*){3,}$", "", question).strip()
        question = re.sub(r"\s+(?:□\s*){2,}.*$", "", question).strip()
        if not question or qid in seen:
            continue
        seen.add(qid)
        questions.append({"id": qid, "text": question, "options": normalize_options()})
    return questions


def build_template_draft(survey_id: str, questions: list[dict[str, Any]], source: str, provider: str) -> dict[str, Any]:
    if not questions:
        raise SystemExit("no questions detected; provide cleaner OCR text or use a Vision provider")
    return {
        "survey_id": survey_id,
        "source": source,
        "provider": provider,
        "draft": True,
        "needs_coordinate_mapping": True,
        "page": {"width": None, "height": None},
        "questions": questions,
        "notes": [
            "Question text/options were drafted from a blank form source.",
            "Fill each option box with [x, y, width, height] before running local OMR on completed responses.",
            "Do not send completed response forms to AI providers unless you explicitly accept that privacy tradeoff.",
        ],
    }


def extract_template_offline(input_path: Path, survey_id: str) -> dict[str, Any]:
    text = input_path.read_text(encoding="utf-8")
    return build_template_draft(survey_id, parse_questions_from_text(text), str(input_path), "offline")


def image_to_data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def call_openai_template_extractor(input_path: Path, survey_id: str, model: str) -> dict[str, Any]:
    """Optional provider for blank-form question recognition.

    Kept dependency-free for easy demos. Requires OPENAI_API_KEY. Tests should use
    `--provider offline` so the repo never needs secrets.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required for --provider openai; use --provider offline for local tests")
    prompt = (
        "Extract the survey questions from this blank survey form. Return only JSON with "
        "a `questions` array. Each item must have `id` like Q1, `text`, and `options` "
        "with keys 1..5 and null values. Do not infer answers; this is a blank form."
    )
    content: list[dict[str, Any]]
    if input_path.suffix.lower() in IMAGE_EXTS:
        content = [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": image_to_data_url(input_path)},
        ]
    else:
        content = [{"type": "input_text", "text": prompt + "\n\n" + input_path.read_text(encoding="utf-8")}]
    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_object"}},
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    chunks: list[str] = []
    for item in raw.get("output", []):
        for part in item.get("content", []):
            if part.get("type") in {"output_text", "text"}:
                chunks.append(part.get("text", ""))
    if not chunks:
        raise SystemExit("OpenAI response did not contain output text")
    data = json.loads("\n".join(chunks))
    questions = data.get("questions", [])
    for q in questions:
        q.setdefault("options", normalize_options())
    return build_template_draft(survey_id, questions, str(input_path), "openai")


def cmd_extract_template(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    survey_id = args.survey_id or input_path.stem.replace(" ", "_").lower()
    if args.provider == "offline":
        draft = extract_template_offline(input_path, survey_id)
    elif args.provider == "openai":
        draft = call_openai_template_extractor(input_path, survey_id, args.model)
    else:
        raise SystemExit(f"unsupported provider: {args.provider}")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote template draft {output}")
    print(f"questions_detected: {len(draft['questions'])}")
    print("needs_coordinate_mapping: true")


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local-first template-based paper survey OMR harness")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-sample", help="create public synthetic sample form/template")
    gen.add_argument("--root", default=".")
    run = sub.add_parser("run", help="read completed form images/PDFs using a template")
    run.add_argument("--template", required=True)
    run.add_argument("--input", required=True)
    run.add_argument("--output", required=True)
    extract = sub.add_parser("extract-template", help="draft template questions from a blank form source")
    extract.add_argument("--provider", choices=["offline", "openai"], default="offline")
    extract.add_argument("--input", required=True, help="OCR text/markdown for offline, or blank form image/text for openai")
    extract.add_argument("--output", required=True)
    extract.add_argument("--survey-id")
    extract.add_argument("--model", default="gpt-4.1-mini")
    args = parser.parse_args(argv)
    if args.command == "generate-sample":
        generate_sample(Path(args.root))
        print("generated synthetic sample")
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "extract-template":
        cmd_extract_template(args)


if __name__ == "__main__":
    main()
