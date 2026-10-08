"""EMANAGER Dialer desktop client.

    python run.py           # normal mode (config.toml)
    python run.py --demo    # UI demo with scripted events, no audio, no backend
    python run.py --selftest  # checks the built-in backend without UI (CI)
"""
import os
import sys

if sys.stdout is None or sys.stderr is None:
    # EmanagerDialer.exe has no console window, so sys.stdout/stderr are None and anything
    # that prints (the Hugging Face download progress bar, warnings) crashes with
    # "'NoneType' object has no attribute 'write'". Our own logging goes to dialer.log.
    sys.stdout = sys.stdout or open(os.devnull, "w", encoding="utf-8")
    sys.stderr = sys.stderr or open(os.devnull, "w", encoding="utf-8")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # the overlay shows download progress itself

if "--selftest" in sys.argv:
    from dialer_client.selftest import main
elif "--demo" in sys.argv:
    from dialer_client.demo import main
else:
    from dialer_client.app import main

if __name__ == "__main__":
    sys.exit(main())
