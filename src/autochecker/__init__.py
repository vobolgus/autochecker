#!/usr/bin/env python3
"""
AutoChecker — LLM-powered math grading CLI (Codex backend).

Runs grading through OpenAI's Codex CLI (`codex exec`) so calls go
against your ChatGPT subscription quota instead of per-token API billing.

Requires:
  - `codex` CLI installed (https://developers.openai.com/codex/cli)
  - One-time `codex login` with your ChatGPT account

Run `autochecker` in any directory containing:
  - A solutions file (*solutions*.pdf)
  - Student submission files (*.jpg, *.png, *.pdf)
"""

import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import fitz  # pymupdf
import openpyxl
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


def _version() -> str:
    try:
        return _pkg_version("autochecker")
    except PackageNotFoundError:
        return "dev"


VERSION = _version()

# ── Config ───────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "autochecker"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    # None → let Codex pick the default model for your ChatGPT plan.
    # Override with e.g. "gpt-5-codex" if you want a specific one.
    "default_model": None,
    "codex_timeout_seconds": 1800,
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                config.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── Theme & console ──────────────────────────────────────────────────

theme = Theme({
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red bold",
    "header": "bold magenta",
    "muted": "dim",
    "score.high": "bold green",
    "score.mid": "bold yellow",
    "score.low": "bold red",
})
console = Console(theme=theme)

# ── Constants ────────────────────────────────────────────────────────

QUESTION_POINTS = {1: 2, 2: 2, 3: 3, 4: 2, 5: 3, 6: 2, 7: 2, 8: 2, 9: 4}
TOTAL_POINTS = sum(QUESTION_POINTS.values())  # 22

GRADING_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {
                str(q): {"type": "integer", "minimum": 0, "maximum": mx}
                for q, mx in QUESTION_POINTS.items()
            },
            "required": [str(q) for q in QUESTION_POINTS],
            "additionalProperties": False,
        },
        "total": {"type": "integer", "minimum": 0, "maximum": TOTAL_POINTS},
        "notes": {"type": "string"},
    },
    "required": ["scores", "total", "notes"],
    "additionalProperties": False,
}

GRADING_PROMPT = f"""\
You are grading a student's math competition submission. The competition has 9 questions on \
linear regression (Gaussian noise, normal equations, ridge regularization).

Point values per question: {json.dumps(QUESTION_POINTS)}
Total: {TOTAL_POINTS} points.

The attached images contain:
  1. The official worked solutions (first pages).
  2. The student's handwritten submission (remaining pages).

GRADING INSTRUCTIONS:
- For each question the student attempted, compare their work against the official solution.
- Award points based on correctness and completeness. Partial credit is allowed.
- If a question is not attempted, give 0 points.
- Be fair but rigorous. Minor notation differences are OK. The key mathematical steps must be present.

Return ONLY the JSON object matching the provided schema. Do not run tools, do not edit files, \
do not ask clarifying questions.
"""

NAME_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "object",
            "additionalProperties": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
                    {"type": "null"},
                ]
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}

NAME_MATCHING_PROMPT = """\
Match student submission filename prefixes to the spreadsheet roster.

Submission prefixes:
{file_groups}

Spreadsheet roster (Name, Surname):
{roster}

Filename prefixes are abbreviated FirstName+LastName (e.g., "AlexKto" → "Alexandros Ktoris"). \
For each prefix, find the best matching student from the roster, or null if no good match exists.

Return ONLY a JSON object of the form {{"matches": {{"Prefix": ["FirstName", "LastName"] or null, ...}}}}. \
Do not run tools, do not edit files.
"""


# ── PDF rasterization ────────────────────────────────────────────────

def render_pdf_pages(pdf_path: Path, out_dir: Path, prefix: str) -> list[Path]:
    """Rasterize every page of a PDF to PNG files in out_dir."""
    doc = fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc, 1):
        pix = page.get_pixmap(dpi=200)
        out = out_dir / f"{prefix}_p{i:02d}.png"
        pix.save(str(out))
        paths.append(out)
    doc.close()
    return paths


def materialize_submission(files: list[Path], out_dir: Path, prefix: str) -> list[Path]:
    """Return on-disk image paths for a student's submission, rasterizing PDFs as needed."""
    result = []
    for i, f in enumerate(files, 1):
        suf = f.suffix.lower()
        if suf == ".pdf":
            result.extend(render_pdf_pages(f, out_dir, f"{prefix}_sub{i}"))
        elif suf in (".jpg", ".jpeg", ".png"):
            result.append(f)
        else:
            raise ValueError(f"Unsupported file: {f}")
    return result


# ── Directory scanning ───────────────────────────────────────────────

def find_solutions_file(directory: Path) -> Path | None:
    for f in directory.iterdir():
        if f.suffix.lower() == ".pdf" and "solution" in f.name.lower():
            return f
    return None


def _collect_submission_files(directory: Path, solutions_file: Path) -> list[Path]:
    out = []
    for f in sorted(directory.iterdir()):
        if f == solutions_file:
            continue
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".pdf"):
            continue
        if f.name.startswith("."):
            continue
        out.append(f)
    return out


def _autodetect_grouping(files: list[Path]) -> str:
    """Pick 'prefix' (many students × multi-page) vs 'per-file' (one-PDF-per-student).

    Heuristic: if all files collapse to a single stripped-digits prefix AND
    there are at least 5 files, each file is almost certainly a separate
    student (e.g., `img-<timestamp>.pdf`). Otherwise use prefix grouping.
    """
    if len(files) < 5:
        return "prefix"
    prefixes = {(f.stem.rstrip("0123456789") or f.stem) for f in files}
    return "per-file" if len(prefixes) == 1 else "prefix"


def find_submissions(directory: Path, solutions_file: Path,
                     mode: str = "auto") -> tuple[dict[str, list[Path]], str]:
    """Return (groups, resolved_mode).

    `mode` is one of 'auto', 'prefix', 'per-file'. With 'auto', the mode is
    chosen by `_autodetect_grouping`. The resolved mode is returned so the
    caller can display it.
    """
    files = _collect_submission_files(directory, solutions_file)
    resolved = _autodetect_grouping(files) if mode == "auto" else mode

    groups: dict[str, list[Path]] = defaultdict(list)
    if resolved == "per-file":
        for f in files:
            groups[f.stem].append(f)
    else:  # 'prefix'
        for f in files:
            stem = f.stem
            prefix = stem.rstrip("0123456789") or stem
            groups[prefix].append(f)
    return dict(groups), resolved


def find_spreadsheet(directory: Path) -> Path | None:
    """Return the first .xlsx file in `directory`, ignoring Excel temp
    files (`~$*.xlsx`). Only the current directory is searched — parents
    are not, to avoid silently picking up unrelated rosters."""
    try:
        candidates = sorted(
            p for p in directory.glob("*.xlsx")
            if not p.name.startswith("~$") and not p.name.startswith(".")
        )
    except OSError:
        candidates = []
    return candidates[0] if candidates else None


# ── Codex wrapper ────────────────────────────────────────────────────

def check_codex_available():
    if shutil.which("codex") is None:
        console.print("[error]`codex` CLI not found.[/] Install from https://developers.openai.com/codex/cli")
        console.print("[muted]Then run `codex login` once to authenticate with your ChatGPT account.[/]")
        sys.exit(1)


def codex_exec(prompt: str, image_paths: list[Path], schema: dict, model: str | None,
               workdir: Path, timeout: int, tag: str) -> dict:
    """Run `codex exec` non-interactively and return the parsed JSON result.

    Uses --output-schema to force structured output and --output-last-message
    to capture the final agent message into a file (rather than parsing stdout).
    """
    schema_path = workdir / f"schema_{tag}.json"
    schema_path.write_text(json.dumps(schema))
    result_path = workdir / f"result_{tag}.json"
    if result_path.exists():
        result_path.unlink()

    # Prompt must precede `-i` flags: clap's `-i <FILE>...` otherwise swallows
    # the positional prompt as another image path.
    cmd = [
        "codex", "exec",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "--output-schema", str(schema_path),
        "--output-last-message", str(result_path),
    ]
    if model:
        cmd.extend(["-m", model])
    cmd.append(prompt)
    if image_paths:
        # Resolve to absolute: cwd is the ephemeral tmpdir, not the caller's cwd.
        abs_imgs = [str(Path(p).resolve()) for p in image_paths]
        cmd.extend(["-i", ",".join(abs_imgs)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"codex exec timed out after {timeout}s")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " | ".join(err[-5:])[:400]
        raise RuntimeError(f"codex exec failed (rc={proc.returncode}): {tail}")

    if not result_path.exists():
        raise RuntimeError("codex exec produced no result file")

    raw = result_path.read_text().strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"codex exec returned invalid JSON: {e}; raw={raw[:200]}")


# ── Grading ──────────────────────────────────────────────────────────

def grade_student(model: str, timeout: int, solutions_paths: list[Path],
                  student_prefix: str, submission_paths: list[Path],
                  workdir: Path) -> dict:
    prompt = GRADING_PROMPT + f"\n\nStudent identifier: {student_prefix}"
    images = solutions_paths + submission_paths
    try:
        result = codex_exec(prompt, images, GRADING_SCHEMA, model, workdir, timeout,
                            tag=f"grade_{student_prefix}")
    except RuntimeError as e:
        return {
            "scores": {str(i): 0 for i in range(1, 10)},
            "total": 0,
            "notes": f"ERROR: {str(e)[:200]}",
        }
    scores = result.get("scores", {})
    result.setdefault("total", sum(int(scores.get(str(q), 0)) for q in QUESTION_POINTS))
    result.setdefault("notes", "")
    return result


def match_names(model: str, timeout: int, file_prefixes: list[str],
                roster: list[tuple[str, str]], workdir: Path
                ) -> dict[str, tuple[str, str] | None]:
    roster_str = "\n".join(f"  - {n} {s}" for n, s in roster)
    groups_str = "\n".join(f"  - {p}" for p in file_prefixes)
    prompt = NAME_MATCHING_PROMPT.format(file_groups=groups_str, roster=roster_str)
    try:
        result = codex_exec(prompt, [], NAME_MATCH_SCHEMA, model, workdir, timeout,
                            tag="name_match")
    except RuntimeError:
        return {p: None for p in file_prefixes}

    mapping = result.get("matches", {}) or {}
    out = {}
    for p in file_prefixes:
        m = mapping.get(p)
        if m and isinstance(m, list) and len(m) == 2:
            out[p] = (str(m[0]), str(m[1]))
        else:
            out[p] = None
    return out


# ── Spreadsheet ──────────────────────────────────────────────────────

def _col_letter(idx: int) -> str:
    """0-based column index → Excel-style letter (0 → A, 1 → B, ...)."""
    n = idx
    letters = ""
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


NAME_HEADERS = ("name", "first name", "firstname", "given", "имя")
SURNAME_HEADERS = ("surname", "last name", "lastname", "family", "фамилия")
SCORE_HEADERS = ("score", "total", "grade", "points", "mark",
                 "оценка", "балл", "итог")

# Fallback column indices (0-based) used when headers can't be detected.
FALLBACK_NAME_COL = 1      # column B
FALLBACK_SURNAME_COL = 2   # column C
FALLBACK_SCORE_COL = 5     # column F


def _detect_roster_columns(ws) -> tuple[int, int, int]:
    """Inspect row 1 for name/surname/score headers. Return 0-based indices;
    fall back to B/C/F if a header can't be matched."""
    name_col = surname_col = score_col = None
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        text = str(cell).strip().lower()
        if name_col is None and any(h == text or h in text for h in NAME_HEADERS):
            # Prefer exact match for "name" over substrings like "surname".
            if "surname" in text or "last" in text or "family" in text or "фамилия" in text:
                pass
            else:
                name_col = i
        if surname_col is None and any(h in text for h in SURNAME_HEADERS):
            surname_col = i
        if score_col is None and any(h in text for h in SCORE_HEADERS):
            score_col = i
    return (
        FALLBACK_NAME_COL if name_col is None else name_col,
        FALLBACK_SURNAME_COL if surname_col is None else surname_col,
        FALLBACK_SCORE_COL if score_col is None else score_col,
    )


def _pick_roster_sheet(wb, preferred: str | None = None):
    """Return the worksheet to use as roster. Prefer `preferred` if given,
    otherwise the first sheet whose row-1 has a detectable name header,
    otherwise the first sheet."""
    if preferred and preferred in wb.sheetnames:
        return wb[preferred]
    for name in wb.sheetnames:
        ws = wb[name]
        header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        for cell in header:
            if cell is None:
                continue
            text = str(cell).strip().lower()
            if any(h in text for h in NAME_HEADERS + SURNAME_HEADERS):
                return ws
    return wb[wb.sheetnames[0]]


def read_roster(spreadsheet_path: Path, sheet_name: str | None = None
                ) -> tuple[list[tuple[str, str]], str, tuple[int, int, int]]:
    """Return (roster rows, resolved sheet name, (name/surname/score cols))."""
    wb = openpyxl.load_workbook(spreadsheet_path)
    try:
        ws = _pick_roster_sheet(wb, sheet_name)
        name_col, surname_col, score_col = _detect_roster_columns(ws)
        roster = []
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            if max(name_col, surname_col) >= len(row):
                continue
            name = row[name_col].value
            surname = row[surname_col].value
            if name and surname:
                roster.append((str(name), str(surname)))
        return roster, ws.title, (name_col, surname_col, score_col)
    finally:
        wb.close()


def write_scores(spreadsheet_path: Path, scores_by_name: dict[tuple[str, str], int],
                 sheet_name: str, cols: tuple[int, int, int]) -> int:
    name_col, surname_col, score_col = cols
    wb = openpyxl.load_workbook(spreadsheet_path)
    try:
        ws = wb[sheet_name]
        filled = 0
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            if max(name_col, surname_col) >= len(row):
                continue
            name = row[name_col].value
            surname = row[surname_col].value
            if name and surname:
                key = (str(name), str(surname))
                if key in scores_by_name:
                    # Extend the row if the score column is beyond current width.
                    if score_col >= len(row):
                        ws.cell(row=row[0].row, column=score_col + 1,
                                value=scores_by_name[key])
                    else:
                        row[score_col].value = scores_by_name[key]
                    filled += 1
        wb.save(spreadsheet_path)
        return filled
    finally:
        wb.close()


# ── UI ───────────────────────────────────────────────────────────────

def score_style(score: int, total: int) -> str:
    ratio = score / total if total else 0
    if ratio >= 0.8:
        return "score.high"
    if ratio >= 0.5:
        return "score.mid"
    return "score.low"


KNOWN_MODELS = [
    (None, "plan default (recommended — Codex picks per your ChatGPT plan)"),
    ("gpt-5-codex", "gpt-5-codex — coding-tuned variant"),
    ("gpt-5.4", "gpt-5.4 — current Codex default on most plans"),
]


def _model_label(model: str | None) -> str:
    return model if model else "plan default"


def render_header(state: dict):
    lines = Text()
    lines.append("  autochecker ", style="bold cyan")
    lines.append(f"v{VERSION}", style="muted")
    lines.append("   math grading · Codex CLI\n\n", style="muted")

    lines.append("  dir      ", style="muted")
    lines.append(f"{state['cwd']}\n", style="info")

    lines.append("  model    ", style="muted")
    lines.append(_model_label(state["model"]), style="bold")
    lines.append("  (via ChatGPT subscription)\n", style="muted")

    sol = state["solutions_file"]
    lines.append("  crit     ", style="muted")
    if sol:
        lines.append(f"{sol.name}\n", style="success")
    else:
        lines.append("none found\n", style="warning")

    groups = state["groups"]
    lines.append("  students ", style="muted")
    if groups:
        n_files = sum(len(f) for f in groups.values())
        mode_hint = "one-per-file" if state.get("grouping") == "per-file" else "by name prefix"
        lines.append(
            f"{len(groups)} students · {n_files} files  ",
            style="success",
        )
        lines.append(f"({mode_hint})\n", style="muted")
    else:
        lines.append("none found\n", style="warning")

    sh = state["spreadsheet"]
    lines.append("  roster   ", style="muted")
    if sh:
        lines.append(f"{sh.name}", style="success")
    else:
        lines.append("not found (name matching disabled)", style="warning")

    console.print(Panel(lines, border_style="cyan", padding=(0, 0)))
    console.print(
        "  [muted]Commands:[/] "
        "[bold]/students[/] · [bold]/crit[/] · [bold]/model[/] · "
        "[bold]/group[/] · [bold]/grade[/] · [bold]/rescan[/] · "
        "[bold]/help[/] · [bold]/quit[/]"
    )


def cmd_help(state: dict):
    tbl = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
    tbl.add_column(style="bold cyan")
    tbl.add_column()
    tbl.add_row("/students", "list discovered student submissions")
    tbl.add_row("/crit", "show path to the solutions/criteria file")
    tbl.add_row("/model", "pick the Codex model (or save a default)")
    tbl.add_row("/group", "switch grouping (prefix / per-file / auto)")
    tbl.add_row("/grade", "run grading against the solutions file")
    tbl.add_row("/rescan", "re-scan the current directory")
    tbl.add_row("/status", "re-print the header")
    tbl.add_row("/help", "show this help")
    tbl.add_row("/quit", "exit (also Ctrl-D)")
    console.print()
    console.print(tbl)


def cmd_students(state: dict):
    groups = state["groups"]
    if not groups:
        console.print("  [warning]No student submissions found in[/] " + str(state["cwd"]))
        return
    tbl = Table(border_style="cyan", title="Students", title_style="bold cyan")
    tbl.add_column("#", style="muted", width=3, justify="right")
    tbl.add_column("Prefix", style="bold")
    tbl.add_column("Files", justify="right")
    tbl.add_column("Names", style="muted")
    for i, (prefix, files) in enumerate(groups.items(), 1):
        names = ", ".join(f.name for f in files)
        tbl.add_row(str(i), prefix, str(len(files)), names)
    console.print()
    console.print(tbl)
    console.print("  [muted]Roster matching runs during /grade.[/]")


def cmd_crit(state: dict):
    sol = state["solutions_file"]
    if not sol:
        console.print(f"  [warning]No *solutions*.pdf found in[/] {state['cwd']}")
        return
    console.print()
    console.print(f"  [muted]solutions/criteria file:[/]")
    console.print(f"  [success]{sol.resolve()}[/]")
    try:
        import fitz as _fitz
        doc = _fitz.open(sol)
        console.print(f"  [muted]{len(doc)} page{'s' if len(doc) != 1 else ''}[/]")
        doc.close()
    except Exception:
        pass


def cmd_model(state: dict):
    config = state["config"]
    current = state["model"]
    console.print()
    console.print(f"  [muted]Current:[/] [bold]{_model_label(current)}[/]")
    console.print()
    tbl = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
    for i, (name, desc) in enumerate(KNOWN_MODELS, 1):
        marker = "[info]*[/]" if name == current else " "
        tbl.add_row(f"  {marker}", f"[muted]{i}.[/]", desc)
    tbl.add_row("   ", f"[muted]{len(KNOWN_MODELS)+1}.[/]", "custom — type your own")
    console.print(tbl)
    choice = Prompt.ask("  [bold]Choose[/]", default=str(1 if current is None else
                        next((i+1 for i, (m, _) in enumerate(KNOWN_MODELS) if m == current), len(KNOWN_MODELS)+1)))
    choice = (choice or "").strip()

    selected: str | None
    if choice.isdigit() and 1 <= int(choice) <= len(KNOWN_MODELS):
        selected = KNOWN_MODELS[int(choice) - 1][0]
    elif choice.isdigit() and int(choice) == len(KNOWN_MODELS) + 1:
        custom = Prompt.ask("  [bold]Model name[/]").strip()
        if not custom:
            console.print("  [muted]Cancelled.[/]")
            return
        selected = custom
    elif choice == "":
        return
    else:
        selected = choice  # treat as custom model name

    state["model"] = selected
    console.print(f"  [success]Model →[/] [bold]{_model_label(selected)}[/]")
    if selected != config.get("default_model"):
        if Confirm.ask(f"  [muted]Save as default?[/]", default=False):
            config["default_model"] = selected
            save_config(config)
            console.print(f"  [success]Saved to[/] {CONFIG_FILE}")


def _rescan(state: dict, mode: str = "auto"):
    cwd = state["cwd"]
    state["solutions_file"] = find_solutions_file(cwd)
    if state["solutions_file"]:
        groups, resolved = find_submissions(cwd, state["solutions_file"], mode=mode)
        state["groups"] = groups
        state["grouping"] = resolved
    else:
        state["groups"] = {}
        state["grouping"] = "prefix"
    state["spreadsheet"] = find_spreadsheet(cwd)


def cmd_rescan(state: dict):
    _rescan(state, mode="auto")
    console.print("  [success]Rescanned.[/]")
    render_header(state)


def cmd_group(state: dict):
    current = state.get("grouping", "prefix")
    console.print()
    console.print(f"  [muted]Current:[/] [bold]{current}[/]")
    console.print(
        "  [muted]Modes:[/] "
        "[bold]prefix[/] (group by name, strip trailing digits) · "
        "[bold]per-file[/] (each file = one student) · "
        "[bold]auto[/] (detect from filenames)"
    )
    choice = Prompt.ask("  [bold]Mode[/]", choices=["prefix", "per-file", "auto"],
                        default=current)
    _rescan(state, mode=choice)
    console.print(f"  [success]Grouping →[/] [bold]{state['grouping']}[/]  "
                  f"([bold]{len(state['groups'])}[/] students)")
    render_header(state)


def run_grading(state: dict):
    sol = state["solutions_file"]
    groups = state["groups"]
    if not sol:
        console.print("  [error]Cannot grade: no solutions file.[/] Run /crit to see expectations.")
        return
    if not groups:
        console.print("  [error]Cannot grade: no student submissions.[/]")
        return

    model = state["model"]
    timeout = int(state["config"].get("codex_timeout_seconds", 1800))
    console.print(
        f"\n  Grading [bold]{len(groups)}[/] students with model "
        f"[bold]{_model_label(model)}[/]"
    )
    if not Confirm.ask("  [bold]Proceed?[/]", default=True):
        console.print("  [muted]Aborted.[/]")
        return

    with tempfile.TemporaryDirectory(prefix="autochecker_") as tmp:
        tmpdir = Path(tmp)
        with console.status("[info]Rendering solutions...[/]", spinner="dots"):
            solutions_paths = render_pdf_pages(sol, tmpdir, "solutions")

        grades = {}
        console.print()
        with Progress(
            SpinnerColumn("dots"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30, complete_style="cyan", finished_style="green"),
            TaskProgressColumn(),
            TextColumn("[muted]|[/]"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task("Grading", total=len(groups))
            for prefix, files in groups.items():
                progress.update(task, description=f"Grading [bold]{prefix}[/]")
                try:
                    submission_paths = materialize_submission(files, tmpdir, prefix)
                    result = grade_student(model, timeout, solutions_paths, prefix,
                                           submission_paths, tmpdir)
                except Exception as e:
                    err_msg = str(e)[:160]
                    progress.console.print(f"  [error]Failed {prefix}:[/] {err_msg}")
                    result = {"scores": {str(i): 0 for i in range(1, 10)}, "total": 0,
                              "notes": f"ERROR: {err_msg}"}
                grades[prefix] = result
                progress.advance(task)
            progress.update(task, description="[success]Grading complete[/]")

        spreadsheet = state["spreadsheet"]
        name_map = None
        roster_sheet = None
        roster_cols = None

        if spreadsheet:
            console.print(f"\n  [info]Spreadsheet:[/] {spreadsheet.name}")
            if Confirm.ask("  [bold]Match names & fill scores?[/]", default=True):
                with console.status("[info]Matching names to roster...[/]", spinner="dots"):
                    roster, roster_sheet, roster_cols = read_roster(spreadsheet)
                    name_map = match_names(model, timeout, list(groups.keys()), roster, tmpdir)
                console.print(
                    f"  [muted]Sheet:[/] [bold]{roster_sheet}[/]  "
                    f"[muted]cols:[/] name={_col_letter(roster_cols[0])} "
                    f"surname={_col_letter(roster_cols[1])} "
                    f"score={_col_letter(roster_cols[2])}  "
                    f"[muted]({len(roster)} rows)[/]"
                )

        scores_by_name = render_results(grades, name_map)

        if name_map:
            unmatched = [p for p in grades if name_map.get(p) is None]
            if unmatched:
                console.print(
                    Panel(
                        f"[warning]{len(unmatched)} unmatched:[/] {', '.join(unmatched)}",
                        border_style="yellow",
                    )
                )

        if name_map and scores_by_name and spreadsheet and roster_sheet and roster_cols:
            if Confirm.ask(f"\n  [bold]Write {len(scores_by_name)} scores to[/] {spreadsheet.name}?", default=True):
                filled = write_scores(spreadsheet, scores_by_name, roster_sheet, roster_cols)
                console.print(f"  [success]Done![/] Wrote {filled} scores.\n")
            else:
                console.print("  [muted]Skipped.[/]\n")
        else:
            console.print()


def render_results(grades: dict, name_map: dict | None = None) -> dict[tuple[str, str], int]:
    has_matches = name_map is not None

    table = Table(border_style="cyan", title="Results", title_style="bold cyan", show_lines=True)
    table.add_column("#", style="muted", width=3, justify="right")
    table.add_column("Student", style="bold", no_wrap=True)
    if has_matches:
        table.add_column("Matched To", no_wrap=True)
    for q in range(1, 10):
        table.add_column(f"Q{q}", justify="center", width=4)
    table.add_column("Total", justify="center", style="bold", width=7)

    scores_by_name = {}
    for i, (prefix, result) in enumerate(grades.items(), 1):
        total = result.get("total", 0)
        scores = result.get("scores", {})

        q_cells = []
        for q in range(1, 10):
            s = scores.get(str(q), 0)
            mx = QUESTION_POINTS[q]
            q_cells.append(f"[{score_style(s, mx)}]{s}[/]/{mx}")

        style = score_style(total, TOTAL_POINTS)
        total_cell = f"[{style}]{total}[/]/{TOTAL_POINTS}"

        row = [str(i), prefix]
        if has_matches:
            match = name_map.get(prefix)
            if match:
                row.append(f"{match[0]} {match[1]}")
                scores_by_name[match] = total
            else:
                row.append("[warning]no match[/]")
        row.extend(q_cells)
        row.append(total_cell)
        table.add_row(*row)

    console.print()
    console.print(table)
    return scores_by_name


# ── Main ─────────────────────────────────────────────────────────────

COMMANDS = {
    "/help": cmd_help,
    "/?": cmd_help,
    "/students": cmd_students,
    "/crit": cmd_crit,
    "/model": cmd_model,
    "/group": cmd_group,
    "/grade": run_grading,
    "/rescan": cmd_rescan,
    "/refresh": cmd_rescan,
    "/status": lambda s: render_header(s),
}


def build_state() -> dict:
    config = load_config()
    cwd = Path.cwd()
    solutions_file = find_solutions_file(cwd)
    if solutions_file:
        groups, grouping = find_submissions(cwd, solutions_file, mode="auto")
    else:
        groups, grouping = {}, "prefix"
    spreadsheet = find_spreadsheet(cwd)
    return {
        "config": config,
        "cwd": cwd,
        "model": config.get("default_model"),
        "solutions_file": solutions_file,
        "groups": groups,
        "grouping": grouping,
        "spreadsheet": spreadsheet,
    }


def main():
    check_codex_available()
    state = build_state()
    render_header(state)

    while True:
        try:
            raw = Prompt.ask("\n[bold cyan]›[/]")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return

        cmd = (raw or "").strip()
        if not cmd:
            continue
        if cmd in ("/quit", "/exit", "/q", "quit", "exit"):
            return
        # Tolerate commands typed without the slash.
        lookup = cmd if cmd.startswith("/") else "/" + cmd.split()[0]
        handler = COMMANDS.get(lookup.split()[0])
        if handler is None:
            console.print(f"  [warning]Unknown command:[/] {cmd}   "
                          f"[muted](try /help)[/]")
            continue
        try:
            handler(state)
        except KeyboardInterrupt:
            console.print("\n  [muted]Interrupted.[/]")


if __name__ == "__main__":
    main()
