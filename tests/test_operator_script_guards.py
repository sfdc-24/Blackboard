from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class OperatorScriptGuardTests(unittest.TestCase):
    def source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_whatsapp_notifier_cannot_override_recipient_or_identity_label(self) -> None:
        source = self.source("scripts/wa_notify.ps1")
        parameter_block = source[source.index("param("):source.index(")\n\n$ErrorActionPreference")]
        self.assertNotIn("[string]$To", parameter_block)
        self.assertNotIn("[switch]$Raw", parameter_block)
        self.assertIn("$recipient = ([string]$cfg.WA_TO", source)
        self.assertIn('$body = "[$Kind - $Tag]`n$Text"', source)

    def test_board_worker_invokes_claude_without_tools_or_persistence(self) -> None:
        source = self.source("scripts/claude_board_worker.ps1")
        for guard in ("'--tools', ''", "'--permission-prompts', 'none'", "'--safe-mode'",
                      "'--strict-mcp-config'", "'--no-session-persistence'",
                      "'--disable-slash-commands'"):
            self.assertIn(guard, source)
        self.assertNotIn("--dangerously-skip-permissions", source)
        self.assertNotIn("'-p', $PromptText", source)
        self.assertIn("$out = $PromptText | & claude @claudeArgs", source)
        self.assertIn("invalid WhatsApp metadata", source)

    def test_experiment_requires_explicit_live_opt_in_and_endpoint(self) -> None:
        source = self.source("scripts/pudding_harness.py")
        self.assertIn('ap.add_argument("--execute-live"', source)
        self.assertIn('if not args.execute_live:', source)
        self.assertIn('url = ENV.get("WEBHOOK_URL")', source)
        self.assertNotIn('ENV.get("WEBHOOK_URL", "https://', source)

    def test_servicenow_credentials_are_limited_to_vendor_origin(self) -> None:
        source = self.source("scripts/snow.ps1")
        self.assertIn("\\.service-now\\.com$", source)
        self.assertIn("$instanceUri.UserInfo", source)
        self.assertIn("$instanceUri.AbsolutePath -ne '/'", source)


if __name__ == "__main__":
    unittest.main()
