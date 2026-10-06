"""EMANAGER Dialer desktop client.

    python run.py           # normal mode (config.toml)
    python run.py --demo    # UI demo with scripted events, no audio, no backend
"""
import sys

if "--demo" in sys.argv:
    from dialer_client.demo import main
else:
    from dialer_client.app import main

if __name__ == "__main__":
    sys.exit(main())
