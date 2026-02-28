#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "rich", "httpx"]
# ///
from codexauth import cli

cli()
