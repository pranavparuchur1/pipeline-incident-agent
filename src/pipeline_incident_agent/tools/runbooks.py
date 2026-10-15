"""BM25 keyword search over runbooks/*.md, one document per `##` section.

The corpus is small (a few dozen sections) and full of exact terms (column names, check ids,
error strings), which keyword search handles well. No embedding model needed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..sandbox.config import REPO_ROOT

RUNBOOK_DIR = REPO_ROOT / "runbooks"
_TOKEN = re.compile(r"[a-z0-9_]+")
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for", "on", "be", "with", "this", "that", "as",
        "are", "by", "if", "not", "do", "from", "at", "when", "what", "how", "why", "i", "we", "you"}  # fmt: skip


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower()):
        if t in STOP:
            continue
        out.append(t)
        if "_" in t:  # order_total also matches "order" and "total"
            out.extend(p for p in t.split("_") if p and p not in STOP)
    return out


@dataclass
class Section:
    id: str  # runbook:<file>#<slug>
    title: str
    text: str


@dataclass
class Hit(Section):
    score: float = 0.0


def split_sections(path: Path) -> list[Section]:
    text = path.read_text(encoding="utf-8")
    doc_title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
    parts = re.split(r"(?m)^## ", text)
    sections = []
    intro = parts[0].strip()
    if intro:
        sections.append(Section(f"runbook:{path.stem}#intro", doc_title, intro))
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        sections.append(Section(f"runbook:{path.stem}#{slug}", f"{doc_title} / {heading.strip()}",
                                f"## {heading.strip()}\n{body.strip()}"))  # fmt: skip
    return sections


class RunbookIndex:
    k1, b = 1.5, 0.75

    def __init__(self, sections: list[Section]):
        self.sections = sections
        self.docs = [Counter(tokens(s.title + " " + s.text)) for s in sections]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lengths) / max(1, len(self.lengths))
        df = Counter(t for d in self.docs for t in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    @classmethod
    def load(cls, directory: Path = RUNBOOK_DIR) -> RunbookIndex:
        return cls([s for p in sorted(directory.glob("*.md")) for s in split_sections(p)])

    def search(self, query: str, k: int = 3) -> list[Hit]:
        q = tokens(query)
        scored = []
        for s, doc, length in zip(self.sections, self.docs, self.lengths, strict=True):
            score = 0.0
            for t in q:
                f = doc.get(t, 0)
                if f:
                    score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            if score > 0:
                scored.append(Hit(s.id, s.title, s.text, score))
        return sorted(scored, key=lambda h: -h.score)[:k]
