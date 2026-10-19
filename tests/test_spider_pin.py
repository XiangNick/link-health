"""固定 Spider、严格哈希和保留上一版产物的回归检查。"""
import hashlib
import json
import unittest
from unittest import mock
from tests import _helpers as H

build = H.build


class SpiderPinTest(unittest.TestCase):
    """上游变更不能替换固定版本，失败不能发布半份配置。"""

    def setUp(self):
        self.cm = H.sandbox()
        self.sb = self.cm.__enter__()
        self.addCleanup(lambda: self.cm.__exit__(None, None, None))
        self.data = H.jar_of_classes(['AppRJ'])
        self.pin = 'https://fixed.test/old.jar;sha256;' + hashlib.sha256(self.data).hexdigest()

    def run_build(self, data):
        """模拟上游恢复并提供另一个包，固定配置始终优先。"""
        self.sb.write_sources({'spider_source': 'A', 'spider_pin': self.pin,
                               'sources': [{'id': 'A', 'name': 'A', 'urls': ['https://upstream.test/config']}]})
        self.sb.write_filter(dict(build.FILTER_DEFAULTS))
        source = {'spider': 'https://upstream.test/new.jar;md5;' + '0' * 32,
                  'sites': H.sites(6, prefix='site', api='csp_AppRJ')}
        net = H.FakeNet(texts={'https://upstream.test/config': json.dumps(source)},
                        blobs={'https://fixed.test/old.jar': data})
        with mock.patch.object(build, 'fetch_text', net.fetch_text), \
             mock.patch.object(build, 'fetch_bytes', net.fetch_bytes), H.fast_time(), H.argv(), H.capture_stdout():
            code = build.main()
        return code, net

    def test_pin_survives_upstream_recovery_and_report_is_static(self):
        code, net = self.run_build(self.data)
        self.assertEqual(code, 0)
        self.assertEqual(net.byte_calls, ['https://fixed.test/old.jar'])
        self.assertEqual(self.sb.read_config()['spider'], self.pin)
        status = self.sb.read_status()['spider']
        self.assertTrue(status['pinned'])
        self.assertTrue(status['hash_verified'])
        self.assertFalse(status['playback_verified'])
        report = self.sb.read_report()
        self.assertIn('未实测首页、搜索或播放', report)
        for claim in ['每个都能跑', '每个都能用', '全部可用', '无必然失败项']:
            self.assertNotIn(claim, report)

    def test_mismatch_and_download_failure_preserve_all_artifacts(self):
        for data in [b'changed', RuntimeError('timeout')]:
            with self.subTest(data=type(data).__name__):
                for path in [self.sb.config, self.sb.status, self.sb.report]:
                    self.sb.write(path, '{"previous":true}')
                code, _ = self.run_build(data)
                self.assertEqual(code, 1)
                for path in [self.sb.config, self.sb.status, self.sb.report]:
                    with open(path) as f:
                        self.assertEqual(f.read(), '{"previous":true}')

    def test_https_md5_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '哈希不一致'):
            build.verify_spider_bytes('https://fixed.test/new.jar;md5;' + '0' * 32, self.data)

    def test_https_md5_and_sha256_match(self):
        for kind in ['md5', 'sha256']:
            value = 'https://fixed.test/old.jar;' + kind + ';' + hashlib.new(kind, self.data).hexdigest()
            self.assertEqual(build.verify_spider_bytes(value, self.data), {'AppRJ'})

    def test_incomplete_or_missing_hash_and_http_are_rejected(self):
        for value in ['https://fixed.test/old.jar', 'https://fixed.test/old.jar;md5;aaa',
                      self.pin + ';extra', self.pin.replace('https:', 'http:')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                build.verify_spider_bytes(value, self.data)

    def test_valid_hash_does_not_approve_native_or_invalid_jar(self):
        for data in [b'not a jar', H.zip_jar(b'invalid dex'),
                     H.zip_jar(extra={'assets/guard.so': b'native'})]:
            value = 'https://fixed.test/x.jar;sha256;' + hashlib.sha256(data).hexdigest()
            with self.subTest(size=len(data)), self.assertRaises(Exception):
                build.verify_spider_bytes(value, data)
