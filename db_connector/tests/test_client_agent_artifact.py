import ast
import pathlib
import unittest


AGENT_SOURCE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "client_agent"
    / "agent_program.py"
)


class ClientAgentArtifactTests(unittest.TestCase):
    def test_agent_has_a_visible_deployment_version(self):
        source = AGENT_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            'AGENT_PROGRAM_VERSION = "2026.11.05-staging-baseline-v1"',
            source,
        )
        self.assertIn("CCD Agent version:", source)

    def test_generated_job_daemon_is_valid_python(self):
        tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
        builder = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "build_job_daemon_script"
        )
        namespace = {}
        exec(
            compile(
                ast.Module(body=[builder], type_ignores=[]),
                str(AGENT_SOURCE),
                "exec",
            ),
            namespace,
        )
        daemon_source = namespace["build_job_daemon_script"](
            "JOB01",
            "test",
            ["SYNC_TO_CCD_REG_BULK"],
            300,
            "job.log",
        )
        compile(daemon_source, "generated_job_daemon.py", "exec")

    def test_staging_baseline_is_confirmed_before_local_cache_is_saved(self):
        source = AGENT_SOURCE.read_text(encoding="utf-8")
        start = source.index(
            'if _target_state.get("target_has_rows"):'
        )
        end = source.index("\n                else:", start)
        adoption_branch = source[start:end]
        confirmation = adoption_branch.index(
            "confirm_registration_baseline_batch("
        )
        completion_gate = adoption_branch.index(
            'not final_result.get("ready_to_finalize")'
        )
        stability_check = adoption_branch.index(
            "complete_registration_baseline_adoption("
        )
        save = adoption_branch.index("save_delta_cache(")
        self.assertLess(confirmation, completion_gate)
        self.assertLess(completion_gate, stability_check)
        self.assertLess(stability_check, save)
        self.assertIn("0 inserted, 0 deleted, 0 updated", adoption_branch)


if __name__ == "__main__":
    unittest.main()
