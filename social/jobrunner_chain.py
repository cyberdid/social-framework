"""Run several commands one after another (stop at the first failure). argv: -- '<json list of argv lists>'."""
import json
import subprocess
import sys

cmds = json.loads(sys.argv[sys.argv.index("--") + 1])
for c in cmds:
    rc = subprocess.call(c)
    if rc:
        sys.exit(rc)
