#!/bin/sh
# Negative control: npx present but the npm registry/package is unavailable.
echo "npm error npx canceled due to missing packages and no YES option" >&2
echo "npm error A complete log of this run can be found in: /tmp/npm/_logs/debug-0.log" >&2
exit 1
