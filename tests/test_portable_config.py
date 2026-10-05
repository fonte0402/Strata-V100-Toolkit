import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from portable_strata import config, guarded_start, service_control


class PortableConfigTests(unittest.TestCase):
    def test_generate_relocatable_paths_with_spaces_and_dry_run(self):
        with tempfile.TemporaryDirectory(prefix="Portable Root With Spaces ") as temp:
            root = Path(temp) / "Relocated Strata Root"
            output = root / "runtime config" / "service.json"
            asset_root = root / "external assets"
            argv = ["generate", "--output", str(output), "--engine-root", str(root / "engine source"),
                    "--engine-exe", str(root / "engine source" / "strata.exe"),
                    "--python", str(root / "python install" / "python.exe"),
                    "--api-root", str(root / "API source"), "--auth-file", str(root / "private" / "auth.json"),
                    "--gpu-uuid", "GPU-TEST-UUID", "--port", "18123",
                    "--asset", f"pack={asset_root / 'pack'}",
                    "--asset", f"native={asset_root / 'model file.gguf'}",
                    "--asset", f"expert_profile={asset_root / 'expert profile.bin'}",
                    "--asset", f"mtp={asset_root / 'mtp'}",
                    "--asset", f"tokenizer={asset_root / 'tokenizer'}"]
            previous = os.environ.get("STRATA_API_KEY")
            os.environ["STRATA_API_KEY"] = "DO_NOT_WRITE_THIS_SENTINEL"
            try:
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    self.assertEqual(config.main(argv), 0)
                self.assertNotIn("DO_NOT_WRITE_THIS_SENTINEL", captured.getvalue())
            finally:
                if previous is None:
                    os.environ.pop("STRATA_API_KEY", None)
                else:
                    os.environ["STRATA_API_KEY"] = previous

            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["managed"]["port"], 18123)
            self.assertEqual(data["managed"]["gpu_uuid"], "GPU-TEST-UUID")
            self.assertTrue(data["managed"]["task_name"].startswith("StrataPortable-18123-"))
            self.assertIn("auto", data["args"])
            self.assertEqual(data["client_policy"]["thinking"], "client-controlled")
            self.assertNotIn("--mmproj", data["args"])
            self.assertTrue((output.parent / "start.ps1").is_file())
            self.assertTrue((output.parent / "stop.ps1").is_file())
            for artifact in (output, output.parent / "start.ps1", output.parent / "stop.ps1"):
                self.assertNotIn("DO_NOT_WRITE_THIS_SENTINEL", artifact.read_text(encoding="utf-8"))
            self.assertIn("python install", (output.parent / "start.ps1").read_text(encoding="utf-8"))

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(config.main(["show-command", "--config", str(output)]), 0)
            plan = json.loads(captured.getvalue())
            self.assertFalse(plan["spawned"])
            self.assertIn("model file.gguf", " ".join(plan["engine_args"]))

            start = config._command(output, "start")
            parsed = guarded_start.parse_args(start[2:])
            self.assertEqual(parsed.python, data["managed"]["python"])
            self.assertEqual(parsed.task_name, data["managed"]["task_name"])
            self.assertEqual(parsed.port, 18123)
            self.assertEqual(start[1], str(Path(guarded_start.__file__).resolve()))

            stop = config._command(output, "stop")
            self.assertEqual(stop[1], str(Path(service_control.__file__).resolve()))
            self.assertEqual(stop[2:4], ["stop", "--state"])
            self.assertIn("--port", stop)
            self.assertIn("--auth-file", stop)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    service_control.main(["stop", "--help"])
            self.assertEqual(raised.exception.code, 0)

    def test_secret_fields_are_rejected_without_blocking_tokenizer(self):
        config._walk_secret_keys({"tokenizer": "external/path", "context": 131072})
        with self.assertRaisesRegex(ValueError, "api_key"):
            config._walk_secret_keys({"api_key": "never allow a key value"})
        with self.assertRaisesRegex(ValueError, "outside the toolkit"):
            config._outside_toolkit(str(Path(config.__file__).resolve().parents[1] / "config.json"), "--output")

    def test_gpu_preflight_keeps_nvidia_reported_name(self):
        with patch.object(service_control, "_run", return_value="GPU-TEST, NVIDIA Test Card, 73"):
            report = service_control._gpu_memory_by_uuid()
        self.assertEqual(report["GPU-TEST"]["name"], "NVIDIA Test Card")
        self.assertEqual(report["GPU-TEST"]["memory_used_mib"], 73)


if __name__ == "__main__":
    unittest.main()
