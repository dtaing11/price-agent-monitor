"""The Windows scheduling paths, exercised without a Windows machine.

Command construction and script generation are pure functions, so they can be
verified anywhere; only the subprocess calls need real Windows.
"""

import unittest
from pathlib import Path
from unittest import mock

from pricemon import cron, schedule


class TestParseTimes(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(cron.parse_times("08:00,20:00"), [(8, 0), (20, 0)])
        self.assertEqual(cron.parse_times("7:30"), [(7, 30)])

    def test_rejects_nonsense(self):
        for bad in ("25:00", "08:99", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cron.parse_times(bad)


class TestWindowsScheduling(unittest.TestCase):
    def test_task_names_are_stable(self):
        self.assertEqual(schedule.windows_task_name(8, 0), "PriceMonitor 0800")
        self.assertEqual(schedule.windows_task_name(20, 30), "PriceMonitor 2030")

    def test_schtasks_command_shape(self):
        commands = schedule.windows_commands(
            [(8, 0), (20, 0)], Path(r"C:\Users\me\.price-monitor\run-check.cmd")
        )
        self.assertEqual(len(commands), 2)
        first = commands[0]
        self.assertEqual(first[:2], ["schtasks", "/Create"])
        self.assertIn("/SC", first)
        self.assertEqual(first[first.index("/SC") + 1], "DAILY")
        self.assertEqual(first[first.index("/ST") + 1], "08:00")
        self.assertEqual(first[first.index("/TN") + 1], "PriceMonitor 0800")
        # /F replaces an existing task rather than failing on a re-install
        self.assertIn("/F", first)
        # the runner path is quoted, since it contains spaces on most machines
        self.assertTrue(first[first.index("/TR") + 1].startswith('"'))

    def test_preview_uses_schtasks_on_windows(self):
        with mock.patch.object(schedule, "is_windows", return_value=True):
            preview = schedule.preview([(8, 0), (20, 0)])
        self.assertIn("schtasks", preview)
        self.assertIn("08:00", preview)
        self.assertIn("20:00", preview)

    def test_preview_uses_cron_elsewhere(self):
        with mock.patch.object(schedule, "is_windows", return_value=False):
            preview = schedule.preview([(8, 0)])
        self.assertIn("0 8 * * *", preview)
        self.assertIn("pricemon check --quiet", preview)

    def test_runner_script_is_batch_and_self_contained(self):
        with mock.patch.object(
            schedule.config_mod, "home", return_value=Path("/tmp/pm-test-home")
        ):
            script = schedule.write_runner_script()
        # Read as bytes: read_text() would normalise CRLF away before we see it.
        raw = script.read_bytes()
        self.assertTrue(script.name.endswith(".cmd"))
        self.assertIn(b"@echo off", raw)
        self.assertIn(b"-m pricemon check --quiet", raw)
        self.assertIn(b"cd /d", raw)  # task working directory
        self.assertIn(b"\r\n", raw)  # CRLF, as Windows batch expects


class TestCronBlock(unittest.TestCase):
    def test_block_round_trips(self):
        lines = cron.build_lines([(8, 0)], Path("/proj"), Path("/log/cron.log"))
        block = cron.render_block(lines)
        existing = "MAILTO=me\n0 5 * * * something-else\n"
        combined = existing + "\n" + block
        # Removing our block must leave a user's own entries untouched.
        self.assertEqual(cron.strip_block(combined).strip(), existing.strip())

    def test_env_is_inline_not_crontab_level(self):
        line = cron.build_lines([(8, 0)], Path("/proj"), Path("/log.log"))[0]
        self.assertTrue(line.startswith("0 8 * * *"))
        self.assertIn('PATH="', line)  # inline, after the cd
        self.assertLess(line.index("cd "), line.index('PATH="'))


if __name__ == "__main__":
    unittest.main()
