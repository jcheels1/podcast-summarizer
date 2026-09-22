"""Startup reporting, so a crash that leaves no traceback still says something.

The deployed app died repeatedly with Streamlit's "Oh no." page and a 503,
and the logs showed the server starting and then simply stopping — no
exception, no message. That is what an OOM kill looks like: the process is
killed outright, so nothing gets a chance to report anything.

Diagnosing that by reasoning about which import is heaviest turned out to
be guesswork, and wrong twice. These few lines print the facts instead:
resident memory, the ceiling it is measured against, and which expensive
libraries are actually loaded, at points either side of the import that was
suspected. Everything goes to stdout, which is where Streamlit Community
Cloud's log viewer reads from.

Cheap enough to leave on permanently — it is a handful of file reads at
startup, and the next time something dies silently the log will already
contain the evidence.
"""

import os
import sys

# Libraries big enough that loading one unnecessarily matters on a container
# with about a gigabyte. faster_whisper pulls the bottom four in with it.
HEAVY_MODULES = (
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "torch",
    "tokenizers",
    "huggingface_hub",
    "transformers",
    "numpy",
    "pandas",
    "pyarrow",
    "yt_dlp",
    "reportlab",
    "av",
)


def resident_memory_mb():
    """This process's resident set size in MB, or None off Linux."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def loaded_heavy_modules():
    return [name for name in HEAVY_MODULES if name in sys.modules]


def report(stage: str) -> None:
    """Print one line describing where startup is and what it has cost.

    Deliberately print() rather than logging: Streamlit Cloud's log viewer
    shows stdout, and this has to survive whatever logging configuration the
    app ends up with.
    """
    bits = [f"[startup] {stage}"]

    rss = resident_memory_mb()
    if rss is not None:
        bits.append(f"rss={rss}MB")

    try:
        from config import available_memory_mb

        limit = available_memory_mb()
        if limit:
            bits.append(f"limit={limit}MB")
            if rss is not None:
                bits.append(f"used={round(100 * rss / limit)}%")
    except Exception:  # noqa: BLE001 - diagnostics must never break startup
        pass

    bits.append(f"modules={len(sys.modules)}")
    heavy = loaded_heavy_modules()
    bits.append("heavy=" + (",".join(heavy) if heavy else "none"))

    # Confirms which build is actually running, which mattered when a fix
    # appeared not to take effect and the real question was whether the
    # deployed code contained it at all.
    bits.append("py=" + ".".join(str(n) for n in sys.version_info[:3]))
    bits.append("pid=" + str(os.getpid()))

    print(" ".join(bits), flush=True)
