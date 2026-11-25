"""允许 `python -m geo_verify` 调用命令行。"""

from geo_verify.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
