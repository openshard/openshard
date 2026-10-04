"""A failed verifier must still reach Receipt creation under Actions' bash -e."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="requires bash and jq")
def test_failed_check_is_recorded_and_next_check_runs():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    match = re.search(r"  run_check\(\) \{.*?\n  \}", script, re.S)
    assert match is not None
    command = "set -euo pipefail\nchecks='[]'\nfailed=0\n" + match.group(0) + '''
run_check "failed test" "test" false
run_check "passed test" "test" true
[[ "$failed" == 1 ]]
echo "$checks"
'''
    result = subprocess.run(["bash", "-c", command], text=True, capture_output=True, check=True)
    checks = json.loads(result.stdout.splitlines()[-1])
    assert [(c["status"], c["exit_code"]) for c in checks] == [("failed", 1), ("passed", 0)]
