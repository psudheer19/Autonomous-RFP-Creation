#!/usr/bin/env python3
"""
FDD Analyzer (Optimized) - Analyzes Feature Description Documents using Claude and outputs
structured RFP requirements as a CSV file.

Token optimizations:
  1. Static instructions moved to the `system` parameter with cache_control=ephemeral.
     After the first API call the prompt is served from cache at ~10% of the normal cost,
     so processing N files costs roughly 1x + (N-1)*0.1x prompt tokens instead of N*1x.
  2. The system prompt itself was trimmed ~70% — same instructions, far fewer words.
  3. Per-call token usage (including cache hits/writes) is printed so you can verify savings.

Usage:
    python analyzer_optimized.py <folder_name> [--prompt smsc|mmsc]
    python analyzer_optimized.py my_files --prompt mmsc

If no folder is specified, the script looks for a folder named 'FDD', 'fdd',
'documents', 'docs', 'input', or 'files' next to this script.
Prompt profiles are defined in prompts.py — add new ones there.

Requirements:
    pip install anthropic
    pip install pypdf          # optional, for PDF files
    pip install python-docx    # optional, for DOCX files

Environment:
    ANTHROPIC_API_KEY must be set.
"""

import argparse
import anthropic
import csv
import json
import sys
import time
from pathlib import Path

from prompts import PROMPTS


FILE_SEPARATOR = "=" * 60


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORIES = [
    "Functional Requirements",
    "Interface & Integration",
    "Performance & Scalability",
    "Operations & Administration",
]

CATEGORY_PREFIXES = {
    "Functional Requirements": "FUNC",
    "Interface & Integration": "INT",
    "Performance & Scalability": "PERF",
    "Operations & Administration": "OAM",
}


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _read_text(file_path: Path) -> str:
    for enc in ("utf-8", "utf-16", "latin-1", "cp1252"):
        try:
            return file_path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def _read_pdf(file_path: Path) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(str(file_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        print("  Warning: pypdf not installed. Run: pip install pypdf")
        return ""
    except Exception as exc:
        print(f"  Warning: could not read PDF {file_path.name}: {exc}")
        return ""


def _read_docx(file_path: Path) -> str:
    try:
        import docx
        doc = docx.Document(str(file_path))
        return "\n".join(p.text for p in doc.paragraphs)
    except ImportError:
        print("  Warning: python-docx not installed. Run: pip install python-docx")
        return ""
    except Exception as exc:
        print(f"  Warning: could not read DOCX {file_path.name}: {exc}")
        return ""


def read_file(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(file_path)
    if suffix in (".docx", ".doc"):
        return _read_docx(file_path)
    return _read_text(file_path)


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def build_combined_message(files: list[tuple[str, str]]) -> str:
    """Concatenate all file contents into one message, clearly delimited."""
    parts = []
    for file_name, content in files:
        parts.append(f"{FILE_SEPARATOR}\nFILE: {file_name}\n{FILE_SEPARATOR}\n{content}")
    return "\n\n".join(parts)


def _strip_fences(raw: str) -> str:
    if raw.startswith("```"):
        lines = raw.splitlines()
        start = 1
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        return "\n".join(lines[start:end])
    return raw


def analyze_all_files(
    client: anthropic.Anthropic,
    files: list[tuple[str, str]],
    system_prompt: str,
) -> list[dict]:
    """
    Make one API call per category pillar, each scoped to just that pillar.
    The combined file content is passed with cache_control so it is only billed
    at full price on the first call; the three remaining calls read it from cache
    at ~10% cost.
    """
    combined = build_combined_message(files)
    print(f"Read {len(files)} file(s) ({len(combined):,} chars total). "
          f"Making {len(CATEGORIES)} category calls...\n")

    all_requirements: list[dict] = []

    for i, category in enumerate(CATEGORIES, 1):
        print(f"  [{i}/{len(CATEGORIES)}] Analyzing: {category}")

        # Retry up to 5 times on transient 429 rate-limit errors.
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                with client.messages.stream(
                    model="claude-opus-4-8",
                    max_tokens=32000,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{
                        "role": "user",
                        "content": [
                            # Large file content — cached after the first call.
                            {
                                "type": "text",
                                "text": combined,
                                "cache_control": {"type": "ephemeral"},
                            },
                            # Small per-call instruction — not cached (changes each call).
                            {
                                "type": "text",
                                "text": (
                                    f"Generate requirements for this category ONLY: {category}\n"
                                    "Return a JSON array. Every element must have "
                                    '"category", "feature_name", and "requirement" keys.'
                                ),
                            },
                        ],
                    }],
                ) as stream:
                    message = stream.get_final_message()
                break  # success — exit the retry loop
            except anthropic.RateLimitError as exc:
                wait = 10 * attempt  # 10s, 20s, 30s, 40s, 50s
                if attempt == max_retries:
                    print(f"    Rate limit hit {max_retries} times — giving up on '{category}'.")
                    raise
                print(f"    Rate limit (attempt {attempt}/{max_retries}): {exc}. "
                      f"Waiting {wait}s before retry...")
                time.sleep(wait)

        usage = message.usage
        cache_read  = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        print(
            f"    Tokens — input: {usage.input_tokens}, output: {usage.output_tokens}"
            + (f", cache write: {cache_write}" if cache_write else "")
            + (f", cache read (saved): {cache_read}" if cache_read else "")
        )

        text_block = next((b for b in message.content if hasattr(b, "text")), None)
        if text_block is None:
            print(f"    Warning: no text block in response for '{category}', skipping.")
            continue
        raw = _strip_fences(text_block.text.strip())

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"    Warning: could not parse JSON for '{category}': {exc}")
            print(f"    First 300 chars: {raw[:300]}")
            continue

        if not isinstance(result, list):
            print(f"    Warning: expected a JSON array for '{category}', got {type(result).__name__}")
            continue

        # Force the category field to match exactly in case the model drifts.
        for req in result:
            req["category"] = category

        print(f"    Extracted {len(result)} requirement(s).")
        all_requirements.extend(result)

    return all_requirements


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_csv(requirements_by_category: dict, output_path: Path, req_prefix: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Requirement ID", "Feature Name", "Requirement Statement", "Compliance Type"])

        first_section = True
        for category in CATEGORIES:
            reqs = requirements_by_category.get(category, [])
            if not reqs:
                continue

            if not first_section:
                writer.writerow(["", "", "", ""])
            first_section = False

            writer.writerow([category, "", "", ""])

            prefix = CATEGORY_PREFIXES[category]

            # Check whether any requirement in this category carries a subcategory.
            has_subcategories = any(req.get("subcategory", "") for req in reqs)

            if has_subcategories:
                # Group by subcategory, preserving insertion order.
                groups: dict[str, list] = {}
                for req in reqs:
                    sub = req.get("subcategory", "").strip() or "Other"
                    groups.setdefault(sub, []).append(req)

                # Write each subcategory block with its own counter.
                global_idx = 1
                for sub, sub_reqs in groups.items():
                    writer.writerow(["", sub, "", ""])  # subcategory header row
                    for req in sub_reqs:
                        writer.writerow([
                            f"REQ-{req_prefix}-{prefix}-{global_idx:03d}",
                            req.get("feature_name", ""),
                            req.get("requirement", ""),
                            "Mandatory",
                        ])
                        global_idx += 1
            else:
                # No subcategories — write flat list as before.
                for idx, req in enumerate(reqs, 1):
                    writer.writerow([
                        f"REQ-{req_prefix}-{prefix}-{idx:03d}",
                        req.get("feature_name", ""),
                        req.get("requirement", ""),
                        "Mandatory",
                    ])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir = Path(__file__).parent

    parser = argparse.ArgumentParser(
        description="Analyze FDD/FRD documents and output structured RFP requirements as CSV.",
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Input folder containing documents (default: auto-detected).",
    )
    parser.add_argument(
        "--prompt",
        choices=list(PROMPTS),
        default="smsc",
        help="Prompt profile to use. Available: %(choices)s (default: %(default)s).",
    )
    args = parser.parse_args()

    prompt_cfg = PROMPTS[args.prompt]
    system_prompt = prompt_cfg["system"]
    req_prefix = prompt_cfg["req_prefix"]
    print(f"Using prompt profile: {prompt_cfg['label']}\n")

    # Resolve the input folder
    if args.folder:
        arg = Path(args.folder)
        fdd_folder = arg if arg.is_absolute() else script_dir / arg
    else:
        candidates = ["FDD", "fdd", "FDDs", "fdds", "documents", "docs", "input", "files"]
        fdd_folder = next(
            (script_dir / name for name in candidates if (script_dir / name).is_dir()),
            None,
        )
        if fdd_folder is None:
            print("No folder found. Usage: python analyzer_optimized.py <folder_name>")
            print("Or create a subfolder named 'FDD' next to this script.")
            sys.exit(1)

    if not fdd_folder.is_dir():
        print(f"Error: '{fdd_folder}' is not a directory.")
        sys.exit(1)

    # Collect files
    supported = {".txt", ".pdf", ".docx", ".doc", ".md", ".csv", ".html", ".xml", ".json", ".rtf"}
    files = sorted(f for f in fdd_folder.iterdir() if f.is_file() and f.suffix.lower() in supported)
    if not files:
        files = sorted(f for f in fdd_folder.iterdir() if f.is_file())
    if not files:
        print(f"No files found in '{fdd_folder}'.")
        sys.exit(1)

    print(f"Found {len(files)} file(s) in '{fdd_folder.name}/'")

    # Read all files first
    loaded: list[tuple[str, str]] = []
    for file_path in files:
        content = read_file(file_path)
        if not content or not content.strip():
            print(f"  Skipping {file_path.name}: empty or unreadable.")
            continue
        print(f"  Read {file_path.name} ({len(content):,} chars)")
        loaded.append((file_path.name, content))

    if not loaded:
        print("No readable files found.")
        sys.exit(1)

    client = anthropic.Anthropic()

    # Single API call across all files
    raw_reqs = analyze_all_files(client, loaded, system_prompt)

    requirements_by_category: dict = {cat: [] for cat in CATEGORIES}
    counts: dict = {}
    for req in raw_reqs:
        cat = req.get("category", "")
        if cat in requirements_by_category:
            requirements_by_category[cat].append(req)
            counts[cat] = counts.get(cat, 0) + 1

    total = sum(counts.values())
    breakdown = ", ".join(f"{v} {k}" for k, v in counts.items() if v > 0)
    print(f"Extracted {total} requirement(s): {breakdown}")

    total_reqs = sum(len(v) for v in requirements_by_category.values())
    if total_reqs == 0:
        print("\nNo requirements extracted from any file.")
        sys.exit(1)

    output_path = script_dir / "requirements_output.csv"
    write_csv(requirements_by_category, output_path, req_prefix)

    print(f"\n{'=' * 55}")
    print(f"Done. Total requirements: {total_reqs}")
    for cat in CATEGORIES:
        reqs = requirements_by_category[cat]
        if reqs:
            prefix = CATEGORY_PREFIXES[cat]
            print(f"  {cat}: {len(reqs)}  "
                  f"(REQ-{req_prefix}-{prefix}-001 … REQ-{req_prefix}-{prefix}-{len(reqs):03d})")
    print(f"\nOutput written to: {output_path}")


if __name__ == "__main__":
    main()
