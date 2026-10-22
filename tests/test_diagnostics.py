import unittest
from backend import diagnostics as diag


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        diag.configure(None)

    def test_redacts_signed_urls_auth_headers_and_structured_secrets(self):
        value = {'causes': [{'message': 'GET https://user:pass@t4.bcbits.com/stream/SECRET?enc=SECRET\nAuthorization: Bearer SECRET\nCookie: session=SECRET'}],
                 'headers': {'Authorization': 'Bearer SECRET'}, 'stream_url': 'SECRET',
                 'message': 'url=/stream/SECRET?token=SECRET'}
        self.assertNotIn('SECRET', str(diag.sanitize(value)))
        self.assertNotIn('pass@', str(diag.sanitize(value)))

    def test_exception_retains_phase_chain_and_source_location(self):
        records = []
        diag.configure(records.append, 'test-request')
        diag.stage('bandcamp_connect')
        try:
            try:
                raise OSError(13, 'Permission denied')
            except OSError as cause:
                raise RuntimeError('Request https://cdn.example/file?token=SECRET failed') from cause
        except RuntimeError as error:
            diag.exception(error)
        record = records[-1]
        self.assertEqual(record['request_id'], 'test-request')
        self.assertEqual(record['details']['phase'], 'bandcamp_connect')
        self.assertEqual(record['details']['causes'][1]['errno'], 13)
        self.assertTrue(record['details']['causes'][0]['frames'])
        self.assertNotIn('SECRET', str(record))
        self.assertEqual(set(record['details']['causes'][0]['frames'][0]), {'file', 'line', 'function'})

    def test_logging_failure_does_not_break_the_worker(self):
        def unavailable(_):
            raise OSError('disk full')
        diag.configure(unavailable)
        diag.log('should_not_raise')
