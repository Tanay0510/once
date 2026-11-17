"""Mask secrets before anything reaches disk. Conservative: prefer over-masking."""
import math
import re

MASK = "[REDACTED]"
MAX_STR = 2000  # stored text is truncated; raw content is opt-in, not default

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)://[^/\s:@]+:[^/\s:@]+@"),
]
_KV = re.compile(
    r"(?i)\b([A-Z0-9_\-]*(?:password|passwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key)[A-Z0-9_\-]*)"
    r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{4,})(['\"]?)"
)
_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_\-]?key|access[_\-]?key|authorization)")


def _entropy(s: str) -> float:
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


_NAMELIKE = re.compile(r"[A-Z]?[a-z0-9.]*|[a-z]+(?:[A-Z][a-z0-9]+)+|[A-Za-z0-9.]*[-_][A-Za-z0-9._-]*"
                       r"|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}.*")


def _pathlike(tok: str, before: str = "") -> bool:
    """A file path, not a base64 secret that happens to contain '/': at least two thirds of its segments read
    like names (lower case, a capitalised word, camelCase, or words joined by '-' or '_'). Base64 scatters
    capitals and digits through every segment, so a key with slashes in it still counts as one token."""
    if tok.count("/") < 2:
        return False
    segs = [s for s in tok.split("/") if s]
    named = sum(1 for s in segs if _NAMELIKE.fullmatch(s))
    return named * 3 >= len(segs) * 2


def redact_text(s: str) -> tuple[str, int]:
    n = 0
    for p in _PATTERNS:
        s, k = p.subn(MASK, s)
        n += k

    def kv(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}{m.group(2)}{MASK}"

    s = _KV.sub(kv, s)

    def secretish(tok: str) -> bool:
        # long, mixed, high-entropy strings; not hex digests of commits (40 hex), sha256 or UUIDs
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", tok, re.I):
            return False
        return _entropy(tok) >= 4.2 and bool(re.search(r"[0-9]", tok)) and bool(re.search(r"[A-Za-z]", tok))

    def ent(m):
        nonlocal n
        tok = m.group(0)
        if _pathlike(tok, m.string[m.start() - 1] if m.start() else ""):
            # A path is judged one segment at a time, so the folders around a secret are kept. Masking the
            # whole path lost the session's working folder whenever it held a UUID or a temp-folder name.
            segs = tok.split("/")
            hit = False
            for i, sg in enumerate(segs):
                if len(sg) >= 32 and secretish(sg):
                    segs[i], hit = MASK, True
            n += hit
            return "/".join(segs)
        if secretish(tok):
            n += 1
            return MASK
        return tok

    s = _CANDIDATE.sub(ent, s)
    if len(s) > MAX_STR:
        s = s[:MAX_STR] + f"...[+{len(s) - MAX_STR} chars]"
    return s, n


def redact(obj, _key: str | None = None):
    """Recursively redact. Returns (clean_obj, count)."""
    if isinstance(obj, str):
        if _key and _SENSITIVE_KEYS.search(_key) and obj:
            return MASK, 1
        return redact_text(obj)
    if isinstance(obj, dict):
        out, n = {}, 0
        for k, v in obj.items():
            out[k], c = redact(v, str(k))
            n += c
        return out, n
    if isinstance(obj, (list, tuple)):
        out, n = [], 0
        for v in obj:
            r, c = redact(v, _key)
            out.append(r)
            n += c
        return out, n
    return obj, 0
