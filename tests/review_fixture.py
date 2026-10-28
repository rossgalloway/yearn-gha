"""Offline fixture for the reusable workflow's real posting shell block."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap

WORKFLOW = Path(__file__).resolve().parents[1]/'.github/workflows/claude-code-review.yml'


def posting_script():
    step = WORKFLOW.read_text().split('      - name: Comment on PR\n', 1)[1]
    return textwrap.dedent(step.split('        run: |\n', 1)[1])



def post_review(report, review_skill="review-pr-lean"):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        execution = root/'execution.json'
        execution.write_text(json.dumps([dict(type='result', is_error=False, result=report)]))
        stub = root/'gh'
        stub.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n'
            'args=sys.argv[1:]\n'
            'body=Path(args[args.index("--body-file")+1]).read_text() if "--body-file" in args else args[args.index("--body")+1]\n'
            'Path(os.environ["CAPTURE"]).write_text(body)\n')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
            REVIEW_SKILL=review_skill, EXEC_FILE=str(execution), RUNNER_TEMP=str(root), CAPTURE=str(root/'posted.md'),
            OAUTH_TOKEN='dummy-oauth-credential', WORKFLOW_TOKEN='dummy-workflow-credential',
            REPO='yearn/example', PR_NUMBER='123', GITHUB_SERVER_URL='https://github.com',
            GITHUB_RUN_ID='456')
        result = subprocess.run(['bash', '-c', posting_script()], env=env, capture_output=True, text=True)
        posted = (root/'posted.md').read_text() if (root/'posted.md').exists() else None
        return result, posted

