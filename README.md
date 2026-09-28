# Arabic Diacritization API — LLM-backed, text-preserving

HTTP service that adds full tashkeel to Arabic text using commercial LLMs (Gemini, OpenAI, Anthropic)
with a hard guarantee: **the service never changes the user's letters, digits, spaces or punctuation — it only adds diacritics.**
Runs as a native Windows Server service (no Docker) with automatic restart.

## Why a guard is needed
LLMs asked to "only add diacritics" still, sometimes: prepend "إليك النص مشكولًا:", normalize إ/أ → ا, swap ة ↔ ه,
drop a word, or change punctuation. Returning that output silently corrupts the customer's text.

`diacritizer/text.py::enforce_diacritics_only` aligns the model output to the input on base characters (LCS),
copies the model's marks only onto letters that match, keeps any marks the input already had, and returns text whose
base is **byte-identical** to the input. The API asserts this invariant on every response.

## Measured results (reproducible: `python -m eval.guard_eval`)
Gold sentences with full tashkeel; each simulated model output is fully diacritized but commits one typical error.

```
failure mode            base preserved  marks recovered     DER
clean                            100%          100.0%   0.000
extra_preamble                   100%           95.7%   0.059
alef_normalized                  100%           97.1%   0.032
letter_substituted               100%           98.6%   0.013
word_dropped                     100%           81.9%   0.180
punct_changed                    100%          100.0%   0.000
mark_order_swapped               100%          100.0%   0.000
```
- **base preserved = 100% in every failure mode** — the customer's text is never altered.
- `marks recovered` = share of letters whose correct diacritics survive the repair; losses are local to the damaged span
  (a dropped word loses only that word's marks, not the sentence).
- DER = diacritic error rate over Arabic letters vs. gold.
- These numbers measure the guard, not model accuracy. Model DER on your data is measured with the same metric from
  captured corrections (`/v1/stats`, `avg_der_on_corrected`) — see *Evaluation loop*.

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/diacritize` | `{"text": "..."}` → `{request_id, text, provider, model, cost_usd, latency_ms, guard, fallbacks}` |
| POST | `/v1/corrections` | `{request_id, corrected, editor?, note?}` — human fix; rejected (422) if it changes letters; stores DER of the model output |
| GET | `/v1/corrections/export` | JSONL of (input, model_output, corrected) — fine-tuning / eval dataset |
| GET | `/v1/stats` | request count, total cost, corrections, average DER |
| GET | `/health` | providers and today's spend |

## Routing and cost control
- `DIAC_PROVIDERS=gemini:gemini-2.5-flash,openai:gpt-4.1-mini,anthropic:claude-sonnet-4-5` — tried in order.
- Next provider is used on HTTP/network errors **or** when the guard had to discard more than 2% of letters.
- `DIAC_DAILY_BUDGET_USD` (default 20) → HTTP 429 when reached; `DIAC_MAX_CHARS` (default 8000) → HTTP 413.
- Per-request cost is computed from provider token usage × price table (`PRICES` in `providers.py`) and logged.

## Windows Server 2019 deployment (native service)
```powershell
# elevated PowerShell, repo root; API keys as machine env vars (never in the repo)
[Environment]::SetEnvironmentVariable("GEMINI_API_KEY","...","Machine")
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY","...","Machine")
.\scripts\install_service.ps1 -Port 8080
```
The script creates a venv, installs deps + pywin32, registers `ArabicDiacritizer` with **automatic start**, configures
**restart on failure** (5 s / 10 s / 30 s, counter reset daily, also on non-crash failures), opens the firewall port and
calls `/health`. Logs go to the Windows Event Log; data to `data\diacritizer.db` (SQLite, WAL mode).
Remove: `.\scripts\uninstall_service.ps1`.

## Evaluation loop
1. Editors fix outputs via `/v1/corrections` (letters locked, only marks editable).
2. `/v1/corrections/export` → JSONL dataset of real errors.
3. Compare models/prompts on that set with `diacritic_error_rate`; the same data seeds later fine-tuning.

## Tests
```bash
pip install -r requirements.txt pytest
pytest -q        # 6 tests: guard, DER, router fallback, budget, API + corrections round-trip (offline echo provider)
```

## Layout
```
diacritizer/text.py         guard, alignment, DER
diacritizer/providers.py    Gemini / OpenAI / Anthropic clients, router, budget
diacritizer/store.py        SQLite persistence for requests and corrections
diacritizer/api.py          FastAPI app
diacritizer/win_service.py  pywin32 service wrapper
scripts/*.ps1               install / uninstall on Windows Server
eval/                       gold set + guard evaluation
```
