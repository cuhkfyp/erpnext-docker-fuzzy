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
            'AGENT_PROGRAM_VERSION = "2026.11.05-governed-staging-v3"',
            source,
        )
        self.assertIn("CCD Agent version:", source)

    @staticmethod
    def _generated_job_daemon():
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
        return namespace["build_job_daemon_script"](
            "JOB01",
            "test",
            ["SYNC_TO_CCD_REG_BULK", "SYNC_TO_CCD_MASTER_BULK"],
            300,
            "job.log",
        )

    def test_generated_job_daemon_is_valid_python(self):
        daemon_source = self._generated_job_daemon()
        compile(daemon_source, "generated_job_daemon.py", "exec")

    def test_v3_fence_precedes_every_pipeline_and_stale_daemon_stops(self):
        daemon_source = self._generated_job_daemon()
        pipeline_start = daemon_source.index("def execute_pipeline():")
        validation = daemon_source.index(
            "validate_registration_fence()", pipeline_start
        )
        first_action = daemon_source.index("execute_step(step, result)", pipeline_start)
        self.assertLess(validation, first_action)
        self.assertIn("except RegistrationFenceError as e:", daemon_source)
        self.assertIn("Registration fence closed:", daemon_source)
        self.assertIn("break\n        except Exception as e:", daemon_source)

    def test_v3_staging_uses_only_generation_fenced_endpoints(self):
        daemon_source = self._generated_job_daemon()
        for method in (
            "inspect_registration_target",
            "get_registration_rows",
            "insert_registration_batch",
            "update_registration_batch",
            "delete_registration_source_keys",
        ):
            self.assertIn(f"{{AGENT_SYNC_API}}.{method}", daemon_source)

        reg_bulk_start = daemon_source.index(
            'if step_upper.startswith("SYNC_TO_CCD_REG_BULK"):'
        )
        master_bulk_start = daemon_source.index(
            'if step_upper.startswith("SYNC_TO_CCD_MASTER_BULK"):'
        )
        self.assertNotIn(
            "/api/method/agent_bulk_sync",
            daemon_source[reg_bulk_start:master_bulk_start],
        )

    def test_main_agent_rediscovers_amended_registrations(self):
        source = AGENT_SOURCE.read_text(encoding="utf-8")
        self.assertIn("def discover_and_launch_new_registrations():", source)
        self.assertIn('CCD_AGENT_DISCOVERY_INTERVAL", "60"', source)
        self.assertIn("time.monotonic() >= next_discovery_at", source)

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

    def test_rejected_master_baseline_batch_never_uses_existence_checkpoint(self):
        source = AGENT_SOURCE.read_text(encoding="utf-8")
        start = source.index('if ins_r.status_code == 200:')
        end = source.index('\n                    else:', start)
        success_branch = source[start:end]
        rejection = success_branch.index('elif msg.get("baseline_adoption"):')
        no_checkpoint = success_branch.index("local Master checkpoint")
        not_advanced = success_branch.index('"was not advanced"')
        ambiguous_recovery = success_branch.index("reconcile_master_rows(chunk)")
        self.assertLess(rejection, no_checkpoint)
        self.assertLess(no_checkpoint, not_advanced)
        self.assertLess(not_advanced, ambiguous_recovery)


if __name__ == "__main__":
    unittest.main()
