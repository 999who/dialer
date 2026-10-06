"""EMANAGER Dialer desktop client.

    python run.py           # normal mode (config.toml)
    python run.py --demo    # UI demo with scripted events, no audio, no backend
    python run.py --selftest  # checks the built-in backend without UI (CI)
"""
import sys

if "--selftest" in sys.argv:
    from dialer_client.selftest import main
elif "--demo" in sys.argv:
    from dialer_client.demo import main
else:
    from dialer_client.app import main

if __name__ == "__main__":
    sys.exit(main())
