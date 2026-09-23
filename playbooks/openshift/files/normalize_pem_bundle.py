#!/usr/bin/env python3
"""Merge PEM blobs, fix literal \\n, dedupe by SHA-256 fingerprint, emit one cert per line block."""
import re
import subprocess
import sys

PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)
SEP = "\x1f"


def parse_chunks(raw: str) -> list[str]:
    certs: list[str] = []
    for chunk in raw.split(SEP):
        chunk = chunk.replace("\\n", "\n")
        certs.extend(PEM_RE.findall(chunk))
    return certs


def fingerprint(pem: str):
    proc = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
        input=(pem.strip() + "\n").encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    line = proc.stdout.decode().strip()
    return line.split("=", 1)[1].strip() if "=" in line else None


def main() -> int:
    raw = sys.stdin.read()
    seen: set[str] = set()
    out: list[str] = []
    for pem in parse_chunks(raw):
        fp = fingerprint(pem)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        out.append(pem.strip())
    if not out:
        return 1
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
