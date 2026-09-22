"""Whether local transcription is offered, based on real memory limits.

The failure this guards against is nasty: local Whisper on a ~1GB hosted
container is killed mid-episode, and an OOM kill takes the whole Streamlit
process with it. The user sees "Oh no. Error running app." with no
traceback and no clue which setting caused it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


real = config.available_memory_mb
try:
    config.available_memory_mb = lambda: 1024
    viable, reason = config.local_transcription_viable()
    check("1GB container is not viable", viable is False)
    check("reason states the measured memory", "1024 MB" in reason, reason)
    check("reason states what's needed", str(config.MIN_LOCAL_TRANSCRIPTION_MB) in reason, reason)
    check("reason explains the app dies, not just the job", "kills the whole app" in reason, reason)

    config.available_memory_mb = lambda: 8192
    check("8GB machine is viable", config.local_transcription_viable()[0] is True)

    # The measured case: Streamlit Cloud reports 3072MB and was still killed
    # transcribing a 90-minute episode, so that must not count as viable.
    config.available_memory_mb = lambda: 3072
    viable, reason = config.local_transcription_viable()
    check("3GB hosted container is NOT viable", viable is False, str(viable))
    check("3GB reason quotes the measurement", "3072 MB" in reason, reason)

    config.available_memory_mb = lambda: 32239
    check("developer laptop stays viable", config.local_transcription_viable()[0] is True)

    config.available_memory_mb = lambda: config.MIN_LOCAL_TRANSCRIPTION_MB
    check("exactly at the threshold is viable", config.local_transcription_viable()[0] is True)

    config.available_memory_mb = lambda: config.MIN_LOCAL_TRANSCRIPTION_MB - 1
    check("one MB under the threshold is not", config.local_transcription_viable()[0] is False)

    # Unmeasurable memory must not disable a machine that might be fine.
    config.available_memory_mb = lambda: None
    viable, reason = config.local_transcription_viable()
    check("unknown memory is treated as viable", viable is True)
    check("unknown memory gives no scary reason", reason == "", reason)
finally:
    config.available_memory_mb = real

# The real reader should agree with itself and be sane on this machine.
measured = config.available_memory_mb()
check("memory is readable or explicitly None", measured is None or measured > 0, str(measured))
if measured is not None:
    check("measured memory is plausible (>128MB)", measured > 128, str(measured))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
