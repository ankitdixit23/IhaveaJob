"""Minimal robots.txt evaluator supporting ``*`` and ``$`` (Google/RFC 9309 style).

Python's ``urllib.robotparser`` does plain prefix matching and ignores
wildcards, which would mis-evaluate rules such as
``Disallow: /about/careers/applications/jobs/results?*&page=``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.compile("^" + regex + ("$" if anchored else ""))


class RobotsPolicy:
    def __init__(self, text: str):
        self.groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow") and current is not None:
                last_was_agent = False
                if value:  # empty Disallow means "allow everything"
                    current.rules.append((key == "allow", value))
            else:
                last_was_agent = False

    def _group_for(self, user_agent: str) -> _Group | None:
        ua = user_agent.lower()
        best: tuple[int, _Group] | None = None
        for group in self.groups:
            for agent in group.agents:
                if agent != "*" and agent in ua:
                    if best is None or len(agent) > best[0]:
                        best = (len(agent), group)
        if best:
            return best[1]
        for group in self.groups:
            if "*" in group.agents:
                return group
        return None

    def is_allowed(self, url: str, user_agent: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        group = self._group_for(user_agent)
        if group is None:
            return True
        # Longest matching rule wins; Allow wins ties (RFC 9309).
        best_len = -1
        allowed = True
        for allow, pattern in group.rules:
            if _pattern_to_regex(pattern).match(path):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len = length
                    allowed = allow
        return allowed
