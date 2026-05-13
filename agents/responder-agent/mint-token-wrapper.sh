#!/usr/bin/env bash
# Wrapper that bash-execs mint-token.py. Works around msmtp's /bin/sh
# context refusing to exec the python3 symlink with EACCES (root-cause
# unclear but the same `python3` works fine when called from bash).
set -e
exec /usr/bin/python3.12 "$(dirname "$0")/mint-token.py" "$@"
