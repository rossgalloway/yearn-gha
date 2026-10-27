"""Validate the actual report publisher without calling GitHub or a model."""
import unittest

from review_fixture import post_review

SECTIONS = '''## Summary
This change corrects a stored-value comparison.

## Issues
No findings.

## How This Was Reviewed
Read the supplied diff and checked the affected callers.
gitconfig-mask: sentinel
'''


class ReviewReportTests(unittest.TestCase):
    def test_both_commands_publish_supported_verdict_forms_with_owned_metadata(self):
        for skill in ['review-pr', 'review-pr-lean']:
            for verdict in ['## Verdict\nAPPROVE', '**Verdict: REQUEST_CHANGES**', 'Verdict: COMMENT']:
                with self.subTest(skill=skill, verdict=verdict):
                    report = verdict+'\n\n'+SECTIONS
                    result, posted = post_review(report, review_skill=skill)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(posted.startswith(report))
                    self.assertIn(f'Publication: posted by the review workflow ({skill}).', posted)
                    self.assertTrue(posted.rstrip().endswith('https://github.com/yearn/example/actions/runs/456'))

    def test_malformed_or_ambiguous_reports_fail_without_publishing_the_body(self):
        cases = [
            SECTIONS,
            '## Verdict\nUNKNOWN\n'+SECTIONS,
            '**Verdict: APPROVE / REQUEST_CHANGES**\n'+SECTIONS,
            '**Verdict: APPROVE**\n## Verdict\nREQUEST_CHANGES\n'+SECTIONS,
            '```markdown\n## Verdict\nAPPROVE\n```\n'+SECTIONS,
            '**Verdict: APPROVE**\n'+SECTIONS.replace('## Summary\nThis change corrects a stored-value comparison.', ''),
            '**Verdict: APPROVE**\n'+SECTIONS.replace('No findings.', ''),
            '**Verdict: APPROVE**\n'+SECTIONS.replace('## How This Was Reviewed\nRead the supplied diff and checked the affected callers.', ''),
        ]
        for skill in ['review-pr', 'review-pr-lean']:
            for report in cases:
                with self.subTest(skill=skill, report=report):
                    result, posted = post_review(report, review_skill=skill)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(posted.startswith('Review blocked:'))
                    self.assertNotIn('This change corrects', posted)
                    self.assertNotIn(report, result.stdout+result.stderr+posted)

    def test_producer_cannot_claim_its_own_publication_status(self):
        for status in ['Not posted to GitHub.', 'Posted to GitHub.', '**Publication: Not posted to GitHub**']:
            with self.subTest(status=status):
                report = '**Verdict: APPROVE**\n'+SECTIONS+'\n'+status
                result, posted = post_review(report)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(posted.startswith('Review blocked:'))
                self.assertNotIn(status, posted)

    def test_quoted_verdict_examples_do_not_override_the_explicit_verdict(self):
        report = '**Verdict: COMMENT**\n'+SECTIONS+'\n> Verdict: REQUEST_CHANGES\n```\nVerdict: APPROVE\n```\n'
        result, posted = post_review(report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(posted.startswith(report))


if __name__ == '__main__':
    unittest.main()
