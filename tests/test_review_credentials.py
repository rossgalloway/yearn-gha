"""Exercise the actual posting step with dummy credentials and an offline gh stub."""
import base64
import json
import re
import textwrap
import unittest

from review_fixture import WORKFLOW, post_review


class CredentialReadTests(unittest.TestCase):
    def test_file_readers_are_denied_and_bash_keeps_the_mask_configuration(self):
        source = WORKFLOW.read_text()
        settings = source.split('          settings: |\n', 1)[1].split('\n      #', 1)[0]
        settings = json.loads(textwrap.dedent(settings))
        # Read rules cover built-in Read/Grep/Glob; tool-specific Glob(path) rules do not.
        self.assertIn('Read(./.git/config)', settings['permissions']['deny'])
        self.assertIn('Read(/${{ github.workspace }}/.git/config)', settings['permissions']['deny'])
        credential = settings['sandbox']['credentials']['files'][0]
        dummy = 'https://x-access-token:dummy-workflow-credential@github.com/yearn/example.git'
        self.assertEqual(re.search(credential['extract'], dummy).group(1), 'dummy-workflow-credential')
        self.assertEqual((credential['mode'], credential['onExtractNoMatch']), ('mask', 'deny'))
        self.assertFalse(settings['sandbox']['allowUnsandboxedCommands'])


    def test_masked_bash_or_denied_fallback_diagnostic_can_be_posted(self):
        for diagnostic in ['sentinel', 'unreadable']:
            with self.subTest(diagnostic=diagnostic):
                report = 'Review body\n' + f'gitconfig-mask: `{diagnostic}`\n'
                result, posted = post_review(report)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(posted, report)

    def test_visible_missing_unknown_or_conflicting_diagnostic_blocks_posting(self):
        for report in ['gitconfig-mask: visible', 'Review body without diagnostic',
                       'gitconfig-mask: unknown', 'gitconfig-mask: sentinel\ngitconfig-mask: visible']:
            with self.subTest(report=report):
                result, posted = post_review(report)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(posted.startswith('Review blocked:'))
                self.assertNotIn(report, result.stdout+result.stderr+posted)

    def test_raw_or_encoded_dummy_credentials_are_never_posted_after_reader_fallback(self):
        for tool in ['Bash', 'Read after Bash sandbox error', 'Grep', 'Glob']:
            for token in ['dummy-oauth-credential', 'dummy-workflow-credential']:
                for value in [token, base64.b64encode(token.encode()).decode()]:
                    with self.subTest(tool=tool, value=value):
                        result, posted = post_review(f'{tool}: {value}\ngitconfig-mask: sentinel')
                        self.assertNotEqual(result.returncode, 0)
                        self.assertTrue(posted.startswith('Review failed:'))
                        self.assertNotIn(value, result.stdout+result.stderr+posted)


if __name__ == '__main__':
    unittest.main()
