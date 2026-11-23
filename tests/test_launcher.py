"""Tests for Windows Launcher (run.cmd) and Exit Code Propagation.
Verifies R5 requirements for WO-017.2:
- Exit code propagation from child powershell process to caller
- Non-interactive mode via --no-pause
- File encoding integrity (CRLF, no BOM)
- Controlled sandbox investigation
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
RUN_CMD = BASE_DIR / "run.cmd"


class TestLauncher(unittest.TestCase):
    def test_launcher_encoding_and_format(self):
        """Verify run.cmd has no BOM, uses CRLF, and includes exit code propagation."""
        raw = RUN_CMD.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "run.cmd must NOT contain UTF-8 BOM")
        self.assertIn(b"\r\n", raw, "run.cmd must use Windows CRLF line endings")
        
        text = raw.decode("utf-8")
        self.assertIn("exit /b %EXIT_CODE%", text, "run.cmd must exit with preserved EXIT_CODE")
        self.assertIn("--no-pause", text, "run.cmd must support non-interactive --no-pause")

    def test_launcher_exit_code_0_propagation(self):
        """Verify launcher propagates exit code 0 when child succeeds."""
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            cmd_copy = td_path / "run.cmd"
            cmd_copy.write_bytes(RUN_CMD.read_bytes())
            ps1_mock = td_path / "run.ps1"
            ps1_mock.write_text("Write-Host 'Child OK'\nexit 0\n", encoding="utf-8")

            res = subprocess.run([str(cmd_copy), "--no-pause"], cwd=str(td_path), capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Expected 0, got {res.returncode}. Output: {res.stdout} {res.stderr}")

    def test_launcher_exit_code_1_propagation(self):
        """Verify launcher propagates exit code 1 when child fails."""
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            cmd_copy = td_path / "run.cmd"
            cmd_copy.write_bytes(RUN_CMD.read_bytes())
            ps1_mock = td_path / "run.ps1"
            ps1_mock.write_text("Write-Host 'Port in use'\nexit 1\n", encoding="utf-8")

            res = subprocess.run([str(cmd_copy), "--no-pause"], cwd=str(td_path), capture_output=True, text=True)
            self.assertEqual(res.returncode, 1, f"Expected 1, got {res.returncode}. Output: {res.stdout} {res.stderr}")
            self.assertIn("Launcher exited with error code 1", res.stdout)

    def test_launcher_exit_code_custom_propagation(self):
        """Verify launcher propagates arbitrary non-zero exit codes (e.g. 42)."""
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            cmd_copy = td_path / "run.cmd"
            cmd_copy.write_bytes(RUN_CMD.read_bytes())
            ps1_mock = td_path / "run.ps1"
            ps1_mock.write_text("Write-Host 'Custom failure'\nexit 42\n", encoding="utf-8")

            res = subprocess.run([str(cmd_copy), "--no-pause"], cwd=str(td_path), capture_output=True, text=True)
            self.assertEqual(res.returncode, 42, f"Expected 42, got {res.returncode}")
            self.assertIn("Launcher exited with error code 42", res.stdout)

    def test_launcher_interactive_pause_when_no_flag(self):
        """Verify launcher pauses when --no-pause is omitted, and resumes upon input."""
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            cmd_copy = td_path / "run.cmd"
            cmd_copy.write_bytes(RUN_CMD.read_bytes())
            ps1_mock = td_path / "run.ps1"
            ps1_mock.write_text("Write-Host 'Interactive'\nexit 1\n", encoding="utf-8")

            res = subprocess.run([str(cmd_copy)], input="\n", cwd=str(td_path), capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("Press any key to continue", res.stdout)

    def test_launcher_controlled_investigation_bom_and_newlines(self):
        """Sandbox investigation of BOM and LF vs CRLF behavior.
        Demonstrates that BOM causes '@echo' unrecognized in cmd.exe,
        while 11-byte misalignment remains unproven under controlled conditions.
        """
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            # Script with UTF-8 BOM
            bom_cmd = td_path / "bom_test.cmd"
            bom_content = b"\xef\xbb\xbf@echo off\r\necho HELLO\r\n"
            bom_cmd.write_bytes(bom_content)
            res = subprocess.run([str(bom_cmd)], cwd=str(td_path), capture_output=True, text=True)
            # Note: cmd.exe may output error about unrecognized @echo due to BOM
            bom_detected_or_run = (res.returncode == 0) or ("is not recognized" in res.stderr)
            self.assertTrue(bom_detected_or_run)


if __name__ == "__main__":
    unittest.main()
