#!/usr/bin/env python3
"""Anonymize real messages before labeling for the acceptance test.

Deterministic and testable: phone numbers, e-mails, URLs and long digit runs become
stable tokens (<PHONE_1>, <EMAIL_1>, ...). Personal names cannot be found reliably by
rules; annotators replace them with <NAME> during labeling (docs/acceptance_data_plan.md).
Input/output: JSONL with a "text" field.
"""
from __future__ import annotations
import json, re, sys

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("URL", re.compile(r"https?://\S+|www\.\S+")),
    ("PHONE", re.compile(r"(?:\+?967[\s-]?)?7[01378]\d(?:[\s-]?\d){6}(?!\d)")),
    ("NUMBER", re.compile(r"(?<!\d)\d{6,}(?!\d)")),
]


class Anonymizer:
    def __init__(self):
        self.maps = {k: {} for k, _ in PATTERNS}

    def _token(self, kind, value):
        m = self.maps[kind]
        if value not in m:
            m[value] = f"<{kind}_{len(m) + 1}>"
        return m[value]

    @staticmethod
    def _canonical(kind, value):
        v = re.sub(r"[\s-]", "", value)
        if kind == "PHONE":
            v = re.sub(r"^\+?967", "", v)   # same subscriber with or without country code
        return v

    def __call__(self, text: str) -> str:
        t = text.translate(AR_DIGITS)
        for kind, rx in PATTERNS:
            t = rx.sub(lambda mo, k=kind: self._token(k, self._canonical(k, mo.group(0))), t)
        return t


if __name__ == "__main__":
    an = Anonymizer()
    for line in sys.stdin:
        if line.strip():
            row = json.loads(line)
            row["text"] = an(row["text"])
            print(json.dumps(row, ensure_ascii=False))
