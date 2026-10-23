#!/usr/bin/env python3
"""Filter known-excluded root public keys from the pinned Mozilla CA bundle.

The upstream data file is the versioned Mozilla bundle published by curl.se.
The exclusions match Homebrew's ca-certificates formula as of 2026-10-20:
AAA Certificate Services and the emSign G3 roots.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

EXCLUDED_SPKI_SHA256 = {
    "bd153ed7b0434f6886b17bce8bbe84ed340c7132d702a8f4fa318f756ecbd6f3",
    "c619e4780ed8e0abeda24ad732567de4383a7ea941338f6d1a7d1b49ac792fe9",
    "8d417db2dd8bf5e3084d1e3f196d583849d81bdd4c00c70b9d39369e96b8c782",
}

CERTIFICATE_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


def spki_sha256(certificate: str) -> str:
    try:
        public_key = subprocess.run(
            ["openssl", "x509", "-inform", "PEM", "-pubkey", "-noout"],
            input=(certificate + "\n").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        der_key = subprocess.run(
            ["openssl", "pkey", "-pubin", "-inform", "PEM", "-outform", "DER"],
            input=public_key.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("host openssl is required to filter the CA bundle") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"openssl could not parse a CA certificate: {message}") from error

    return hashlib.sha256(der_key.stdout).hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} INPUT.pem OUTPUT.pem", file=sys.stderr)
        return 2

    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    contents = source.read_text(encoding="ascii")
    certificates = CERTIFICATE_RE.findall(contents)
    if not certificates:
        raise RuntimeError(f"no PEM certificates found in {source}")

    kept: list[str] = []
    excluded = 0
    seen_certificates: set[str] = set()

    for certificate in certificates:
        normalized = certificate.strip() + "\n"
        fingerprint = hashlib.sha256(normalized.encode("ascii")).hexdigest()
        if fingerprint in seen_certificates:
            continue
        seen_certificates.add(fingerprint)

        if spki_sha256(normalized) in EXCLUDED_SPKI_SHA256:
            excluded += 1
            continue
        kept.append(normalized)

    if len(kept) < 100:
        raise RuntimeError(f"refusing to install unexpectedly small CA bundle ({len(kept)} certificates)")

    destination.write_text("\n".join(kept), encoding="ascii")
    print(
        f"CA bundle filtered: kept {len(kept)} unique certificates; "
        f"excluded {excluded} blocked public keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
