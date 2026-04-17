# autochecker

LLM-powered math grading CLI. Runs everything through OpenAI's **Codex CLI**,
so calls bill against your **ChatGPT subscription** instead of the OpenAI API.

Given a folder containing a solutions PDF and a pile of student submission
scans, it grades each student against the solutions, shows a scorecard, and
(optionally) writes totals back to a roster spreadsheet.

---

## Requirements

- Python **3.11+**
- [Codex CLI](https://developers.openai.com/codex/cli) — `brew install codex` or see upstream docs
- A ChatGPT plan that includes Codex (Plus, Pro, Business, Edu, Enterprise)
- `pipx` (recommended) or plain `pip`

One-time Codex auth:

```bash
codex login      # sign in with your ChatGPT account
```

## Install

```bash
# editable install — tracks changes in src/
pipx install --editable /path/to/AutoChecker
```

Or with plain pip inside a venv:

```bash
pip install -e /path/to/AutoChecker
```

## Quick start

Put a solutions PDF and student scans into a directory, then run `autochecker`
from that directory:

```
mathweek1/
├── linear_regression_solutions.pdf   # filename must contain "solution"
├── AlexKto1.jpg                      # student files — prefix = student id
├── AlexKto2.jpg
├── AlexisTsan1.jpg
├── AlexisTsan2.jpg
├── MarizaPas1.jpg
├── ...
└── PetrosVour.pdf                    # PDFs are rasterized automatically
```

```bash
cd mathweek1
autochecker
```

You'll get an interactive REPL with a status header and slash-commands.

## REPL commands

| Command        | What it does                                        |
| -------------- | --------------------------------------------------- |
| `/status`      | re-print the header (dir, model, solutions, students) |
| `/students`    | list discovered submissions with their filenames    |
| `/crit`        | show the path + page count of the solutions file    |
| `/model`       | pick the Codex model (plan default / gpt-5-codex / custom) |
| `/grade`       | run grading end-to-end                              |
| `/rescan`      | re-read the current directory                       |
| `/help`        | show the command list                               |
| `/quit`        | exit (Ctrl-D also works)                            |

Commands also work without the slash (`grade`, `help`, ...).

## How file grouping works

For each file in the grading directory, the filename **stem** with trailing
digits stripped is used as the student prefix:

- `AlexKto1.jpg`, `AlexKto2.jpg` → group `AlexKto`
- `PetrosVour.pdf` (no trailing digit) → group `PetrosVour`

Supported extensions: `.jpg`, `.jpeg`, `.png`, `.pdf`. PDFs are rendered to
PNGs at 200 DPI before being sent to the model.

## Grading rubric

Hardcoded for the linear-regression problem set (9 questions, 22 points):

| Q1 | Q2 | Q3 | Q4 | Q5 | Q6 | Q7 | Q8 | Q9 |
|----|----|----|----|----|----|----|----|----|
| 2  | 2  | 3  | 2  | 3  | 2  | 2  | 2  | 4  |

To change the rubric, edit `QUESTION_POINTS` in `src/autochecker/__init__.py`
— the JSON schema for the model response is derived from it.

## Spreadsheet integration (optional)

If any `.xlsx` roster is found in the current directory, `autochecker` will:

1. Pick the first sheet that has a recognizable name header (or the first
   sheet if none match).
2. Detect **name / surname / score** columns from the header row. Recognized
   headers include `name`, `first name`, `surname`, `last name`, `score`,
   `total`, `grade`, `points`, and their Russian equivalents (`имя`,
   `фамилия`, `балл`, `оценка`). Fallback: columns **B / C / F**.
3. Ask Codex to match each student file-prefix to a roster row.
4. On confirmation, write each student's total into the detected score column.

The resolved sheet name and column letters are printed before matching so
you can verify. Matched names appear in the results table; unmatched
prefixes are listed in a warning panel so you can rename files and re-run.

## Configuration

Config lives at `~/.config/autochecker/config.json`:

```json
{
  "default_model": null,
  "codex_timeout_seconds": 1800
}
```

- **`default_model`** — Codex model name, or `null` to let Codex pick the
  default for your plan (currently `gpt-5.4`). Known alternatives:
  `gpt-5-codex`. Note: `gpt-5` is **not** available via ChatGPT-account auth.
- **`codex_timeout_seconds`** — per-call subprocess timeout. Each student
  typically takes 30–60 s at `reasoning effort: high`.

`/model` updates this file when you pick "save as default".

## How it works under the hood

For each student, autochecker shells out to:

```
codex exec \
  --sandbox read-only --skip-git-repo-check --ephemeral --color never \
  --output-schema <schema.json> \
  --output-last-message <result.json> \
  [-m MODEL] \
  "<grading prompt>" \
  -i img1,img2,img3,...
```

- `--output-schema` forces the model to emit JSON matching a strict schema
  (per-question scores, total, notes) — no fragile stdout parsing.
- `--output-last-message` writes the final agent reply to a file.
- `--sandbox read-only` keeps the agent from editing anything on disk.
- Everything runs in a `tempfile.TemporaryDirectory` so nothing leaks into
  your working tree.

## Troubleshooting

**`codex CLI not found`** — install Codex (`brew install codex`) and run
`codex login`.

**`The '...' model is not supported when using Codex with a ChatGPT account`**
— the configured model isn't on your plan. `/model` → "plan default", or
pick `gpt-5-codex`.

**942 models available / old LiteLLM prompt** — a stale version of the CLI
is on your `PATH`. `which autochecker` should point at the pipx shim
(`~/.local/bin/autochecker`) or the project venv. Reinstall:

```bash
pipx install --force --editable /path/to/AutoChecker
```

**Grading is slow** — Codex defaults to `reasoning effort: high`. Expect
30–60 s per student. ChatGPT-plan Codex has weekly rate limits; a 15-student
class run fits comfortably on Plus.

**Student shows `ERROR: ...` in notes** — check the error text in the
results table. Common causes: unreadable scan, subprocess timeout, Codex
returning invalid JSON (rare with `--output-schema`).

## Files

```
src/autochecker/__init__.py   # everything: REPL, Codex wrapper, grading, IO
pyproject.toml                # deps: openpyxl, pymupdf, rich
```

## License

[MIT](LICENSE) © Svyatoslav Suglobov
