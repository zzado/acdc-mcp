import argparse
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("directory", nargs="?", type=Path, default=Path("secrets"))
parser.add_argument("--app-uid", type=int, default=10001)
args = parser.parse_args()
directory = args.directory
directory.mkdir(mode=0o711, parents=True, exist_ok=True)

values = {"garage_rpc": secrets.token_hex(32), "token_pepper": secrets.token_hex(32),
          "gateway_secret": secrets.token_hex(32)}
for key in ("staging", "final", "worker"):
    values[f"{key}.id"] = f"GK{secrets.token_hex(12)}"
    values[f"{key}.secret"] = secrets.token_hex(32)
for name, value in values.items():
    try:
        descriptor = os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        continue
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as out:
        out.write(value + "\n")
        if hasattr(os, "fchown") and (name != "garage_rpc") and os.geteuid() == 0:
            os.fchown(out.fileno(), args.app_uid, -1)
