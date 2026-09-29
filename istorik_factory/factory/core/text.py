"""Работа с текстом сценария: предложения, слова, веса для оценки длительности речи."""
from __future__ import annotations

import re

_ABBR = ("г", "гг", "в", "вв", "т.е", "т.д", "т.п", "др", "им", "ок", "н.э", "до н.э", "св", "см", "тыс", "млн", "млрд")
WORD_RE = re.compile(r"[\wЀ-ӿ'-]+", re.U)


def words(text: str) -> int:
    return len(WORD_RE.findall(text))


def split_sentences(text: str) -> list[str]:
    """Разбить абзацы на предложения (учитывая «г.», «в.», «н.э.», многоточия, кавычки)."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        buf = ""
        tokens = re.split(r"(?<=[.!?…])(\s+)", para)
        for tok in tokens:
            if not tok:
                continue
            if tok.isspace():
                buf += tok
                continue
            buf += tok
            stripped = buf.rstrip()
            last = re.findall(r"([\w.]+)\.$", stripped)
            if last and last[0].lower().rstrip(".") in _ABBR:
                continue
            if re.search(r"\d\.$", stripped) and not re.search(r"\d{3,4}\.$", stripped):
                continue
            if re.search(r"[.!?…][»\")]*$", stripped):
                out.append(stripped.strip())
                buf = ""
        if buf.strip():
            out.append(buf.strip())
    return [s for s in out if WORD_RE.search(s)]


def speech_weight(text: str) -> float:
    """Относительная «длительность произнесения»: буквы + паузы на знаках препинания + числа длиннее."""
    letters = len(re.findall(r"[^\W\d_]", text, re.U))
    digits = len(re.findall(r"\d", text)) * 2.6
    pauses = len(re.findall(r"[,;:—–-]", text)) * 3 + len(re.findall(r"[.!?…]", text)) * 6
    return max(1.0, letters + digits + pauses)


def split_long_sentence(text: str, max_words: int) -> list[str]:
    """Разделить слишком длинное предложение по запятым/тире на части ~ равной длины."""
    if words(text) <= max_words:
        return [text]
    pieces = re.split(r"(?<=[,;:—])\s+", text)
    parts, buf = [], ""
    target = max(6, max_words * 0.6)
    for pc in pieces:
        cand = (buf + " " + pc).strip()
        if buf and words(cand) > max_words:
            parts.append(buf)
            buf = pc
        else:
            buf = cand
            if words(buf) >= target:
                parts.append(buf)
                buf = ""
    if buf:
        if parts and words(buf) < 4:
            parts[-1] = parts[-1] + " " + buf
        else:
            parts.append(buf)
    final = []
    for p in parts:  # если запятых нет — режем по словам
        if words(p) > max_words * 1.5:
            ws = p.split()
            n = -(-len(ws) // max_words)
            step = -(-len(ws) // n)
            final += [" ".join(ws[i:i + step]) for i in range(0, len(ws), step)]
        else:
            final.append(p)
    return final
