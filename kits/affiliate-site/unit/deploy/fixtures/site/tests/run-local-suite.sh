#!/usr/bin/env bash
# Stand-in for the kit gate runner: records its arguments, passes unless FAKE_SUITE_EXIT says otherwise.
echo "fake suite ran in $(pwd) with: $*"
exit "${FAKE_SUITE_EXIT:-0}"
