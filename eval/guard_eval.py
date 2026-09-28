"""Measures the diacritics-only guard against realistic LLM failure modes.

For each gold sentence we simulate a model output that is fully diacritized but
also commits one typical error, run the guard, and measure:
  - base_preserved: output letters/spaces/punctuation identical to input (must be 100%)
  - marks_recovered: share of gold letters whose marks survived the repair
  - DER vs gold on the repaired text
Run:  python -m eval.guard_eval
"""
import random
import re

from diacritizer.text import diacritic_error_rate, enforce_diacritics_only, strip_diacritics

FAILURES = {
    "clean": lambda s: s,
    "extra_preamble": lambda s: "إليك النص مشكولًا:\n" + s,
    "alef_normalized": lambda s: s.replace("إ", "ا").replace("أ", "ا"),
    "letter_substituted": lambda s: s.replace("ة", "ه", 1),
    "word_dropped": lambda s: " ".join(w for i, w in enumerate(s.split(" ")) if i != 1),
    "punct_changed": lambda s: s.replace(".", "،"),
    "mark_order_swapped": lambda s: re.sub("(ّ)([َُِ])", r"\2\1", s),
}


def main():
    gold = [l.strip() for l in open("eval/gold.txt", encoding="utf-8") if l.strip()]
    print(f"{'failure mode':22} {'base preserved':>15} {'marks recovered':>16} {'DER':>7}")
    for name, corrupt in FAILURES.items():
        ok = rec = total = 0
        ders = []
        for g in gold:
            plain = strip_diacritics(g)
            fixed, info = enforce_diacritics_only(plain, corrupt(g))
            ok += strip_diacritics(fixed) == plain
            ders.append(diacritic_error_rate(g, fixed))
            letters = sum(1 for c in plain if "ء" <= c <= "ي")
            total += letters
            rec += letters * (1 - ders[-1])
        print(f"{name:22} {ok / len(gold):>14.0%} {rec / total:>15.1%} {sum(ders) / len(ders):>7.3f}")


if __name__ == "__main__":
    main()
