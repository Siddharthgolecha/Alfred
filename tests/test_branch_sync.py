"""Local Git integration tests for the safe main -> intrinsic_jetson merge script."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import textwrap
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/"scripts"/"sync_main_into_jetson.sh"


def run(*args,cwd,check=True):
    return subprocess.run(args,cwd=str(cwd),capture_output=True,text=True,check=check)


class GitBranchSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.work=root/"work"
        self.remote=root/"origin.git"
        self.work.mkdir()
        run("git","init","--bare","--initial-branch=main",str(self.remote),cwd=root)
        run("git","init","-b","main",cwd=self.work)
        run("git","config","user.name","Test Bot",cwd=self.work)
        run("git","config","user.email","test@example.invalid",cwd=self.work)
        run("git","remote","add","origin",str(self.remote),cwd=self.work)
        (self.work/"shared.txt").write_text("base\n")
        run("git","add",".",cwd=self.work)
        run("git","commit","-m","initial",cwd=self.work)
        run("git","push","origin","main",cwd=self.work)
        run("git","switch","-c","intrinsic_jetson",cwd=self.work)
        (self.work/"jetson.txt").write_text("unique Jetson work\n")
        run("git","add",".",cwd=self.work)
        run("git","commit","-m","jetson only",cwd=self.work)
        run("git","push","origin","intrinsic_jetson",cwd=self.work)
        run("git","switch","main",cwd=self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def advance_main(self,filename="general.txt",value="main feature\n"):
        (self.work/filename).write_text(value)
        run("git","add",".",cwd=self.work)
        run("git","commit","-m","main advanced",cwd=self.work)
        run("git","push","origin","main",cwd=self.work)

    def sync(self,check=True):
        return run("bash",str(SCRIPT),cwd=self.work,check=check)

    def test_clean_merge_preserves_jetson_history_and_main_changes(self):
        self.advance_main()
        run("git","switch","intrinsic_jetson",cwd=self.work)
        merged=self.sync()
        self.assertIn("chore(sync)",merged.stdout)
        self.assertTrue((self.work/"jetson.txt").is_file())
        self.assertTrue((self.work/"general.txt").is_file())
        run("git","merge-base","--is-ancestor","origin/main","HEAD",cwd=self.work)
        self.assertEqual(run("git","rev-list","--parents","-n","1","HEAD",
                             cwd=self.work).stdout.count(" "),2)
        self.assertEqual(run("git","status","--porcelain",cwd=self.work).stdout,"")

    def test_idempotent_when_main_is_already_merged(self):
        self.advance_main()
        run("git","switch","intrinsic_jetson",cwd=self.work)
        self.sync()
        # The workflow pushes the merge before the next run; a local-only
        # unpublished merge must be rejected as an unexpected target advance.
        run("git","push","origin","intrinsic_jetson",cwd=self.work)
        before=run("git","rev-parse","HEAD",cwd=self.work).stdout
        result=self.sync()
        self.assertIn("already included",result.stdout)
        self.assertEqual(before,run("git","rev-parse","HEAD",cwd=self.work).stdout)

    def test_conflict_aborts_without_destroying_jetson_branch(self):
        self.advance_main(filename="shared.txt",value="main changes\n")
        run("git","switch","intrinsic_jetson",cwd=self.work)
        (self.work/"shared.txt").write_text("jetson changes\n")
        run("git","add",".",cwd=self.work)
        run("git","commit","-m","jetson changed same line",cwd=self.work)
        run("git","push","origin","intrinsic_jetson",cwd=self.work)
        before=run("git","rev-parse","HEAD",cwd=self.work).stdout
        failed=self.sync(check=False)
        self.assertEqual(failed.returncode,4)
        self.assertIn("Merge conflict",failed.stderr)
        self.assertEqual(before,run("git","rev-parse","HEAD",cwd=self.work).stdout)
        self.assertEqual((self.work/"shared.txt").read_text(),"jetson changes\n")
        self.assertEqual(run("git","status","--porcelain",cwd=self.work).stdout,"")

    def test_wrong_branch_refuses_to_sync(self):
        failure=self.sync(check=False)
        self.assertEqual(failure.returncode,2)
        self.assertIn("expected 'intrinsic_jetson'",failure.stderr)

    def test_remote_target_advance_detected_and_not_overwritten(self):
        self.advance_main()
        run("git","switch","intrinsic_jetson",cwd=self.work)
        # another repository actor advances the remote target.
        root=Path(self.tmp.name)
        other=root/"other"
        run("git","clone","-b","intrinsic_jetson",str(self.remote),str(other),cwd=root)
        run("git","config","user.name","Other Actor",cwd=other)
        run("git","config","user.email","other@example.invalid",cwd=other)
        (other/"other.txt").write_text("concurrent update\n")
        run("git","add",".",cwd=other)
        run("git","commit","-m","concurrent",cwd=other)
        run("git","push","origin","intrinsic_jetson",cwd=other)
        failed=self.sync(check=False)
        self.assertEqual(failed.returncode,3)
        self.assertIn("advanced since checkout",failed.stderr)

    def test_workflow_bootstraps_helper_from_main_before_jetson_checkout(self):
        workflow=(Path(__file__).resolve().parents[1]/
                  ".github/workflows/sync-main-to-intrinsic-jetson.yml").read_text()
        self.assertIn("ref: main",workflow)
        self.assertNotIn("ref: ${{ github.sha }}",workflow)
        self.assertLess(workflow.index("Enforce main-only synchronization"),
                        workflow.index("Check out trusted sync helper from main"))
        self.assertLess(workflow.index("Check out trusted sync helper from main"),
                        workflow.index("Check out intrinsic_jetson without discarding its history"))
        self.assertLess(workflow.index("Stage sync helper outside the worktree"),
                        workflow.index("Check out intrinsic_jetson without discarding its history"))
        self.assertIn('cp scripts/sync_main_into_jetson.sh "$RUNNER_TEMP/alfred-sync-main-into-jetson.sh"',
                      workflow)
        self.assertIn('run: bash "$RUNNER_TEMP/alfred-sync-main-into-jetson.sh"',workflow)
        self.assertNotIn("run: bash scripts/sync_main_into_jetson.sh",workflow)

    def test_non_main_workflow_dispatch_is_rejected_before_checkout(self):
        """Execute the real inline workflow guard for main and non-main refs."""
        workflow=(Path(__file__).resolve().parents[1]/
                  ".github/workflows/sync-main-to-intrinsic-jetson.yml").read_text()
        guard=workflow.split("      - name: Enforce main-only synchronization\n",1)[1].split(
            "      - name: Check out trusted sync helper from main\n",1)[0]
        script=textwrap.dedent(guard.split("        run: |\n",1)[1])
        self.assertIn('WORKFLOW_REF: ${{ github.ref }}',guard)
        self.assertIn("exit 1",script)
        for ref,expected in (("refs/heads/main",0),
                             ("refs/heads/intrinsic_jetson",1),
                             ("refs/heads/fix/sync-workflow-bootstrap-dedup",1),
                             ("refs/tags/v0.1",1)):
            with self.subTest(dispatch_ref=ref):
                result=subprocess.run(
                    ["bash","-c",script],
                    env={**os.environ,"WORKFLOW_REF":ref},
                    capture_output=True,text=True,check=False,
                )
                self.assertEqual(result.returncode,expected,result.stderr)
                if expected:
                    self.assertIn("only allowed from refs/heads/main",result.stdout)

    def test_workflow_consolidates_sync_incidents_and_verifies_before_closure(self):
        workflow=(Path(__file__).resolve().parents[1]/
                  ".github/workflows/sync-main-to-intrinsic-jetson.yml").read_text()
        self.assertIn('startswith("[sync] main to intrinsic_jetson blocked")',workflow)
        self.assertIn('gh issue edit "$found" -R "$GITHUB_REPOSITORY" --title "$title" --body-file',workflow)
        self.assertNotIn('--label "maestaris:task"',workflow)
        self.assertIn("git merge-base --is-ancestor refs/remotes/origin/main refs/remotes/origin/intrinsic_jetson",
                      workflow)
        self.assertLess(workflow.index("Verify published Jetson contains latest main"),
                        workflow.index("Close resolved sync blockers only after remote verification"))
        self.assertIn("steps.verify.outcome == 'failure'",workflow)


if __name__=="__main__":
    unittest.main()
