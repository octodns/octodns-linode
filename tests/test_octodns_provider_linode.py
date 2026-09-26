#
#
#

from os.path import dirname, join
from unittest import TestCase
from unittest.mock import Mock, call

from requests import HTTPError
from requests_mock import ANY
from requests_mock import mock as requests_mock

from octodns.provider import SupportsException
from octodns.provider.yaml import YamlProvider
from octodns.record import Record
from octodns.zone import Zone

from octodns_linode import LinodeClientUnauthorized, LinodeProvider

DOMAINS_BASE = 'https://api.linode.com/v4/domains'


class TestLinodeProvider(TestCase):
    expected = Zone('unit.tests.', [])
    source = YamlProvider(
        'test', join(dirname(__file__), 'config'), escaped_semicolons=False
    )
    source.populate(expected)

    # Our test suite differs a bit, add our NS and remove the simple one,
    # Linode doesn't allow arbitrary/IP-like NS targets
    expected.add_record(
        Record.new(
            expected,
            'under',
            {
                'ttl': 3600,
                'type': 'NS',
                'values': ['ns1.unit.tests.', 'ns2.unit.tests.'],
            },
        )
    )
    for record in list(expected.records):
        if record.name == 'sub' and record._type == 'NS':
            expected.remove_record(record)
            break

    def _mock_zone(self, mock):
        with open('tests/fixtures/linode-domains-page-1.json') as fh:
            mock.get(f'{DOMAINS_BASE}?page=1', text=fh.read())
        with open('tests/fixtures/linode-domains-page-2.json') as fh:
            mock.get(f'{DOMAINS_BASE}?page=2', text=fh.read())
        with open('tests/fixtures/linode-records-page-1.json') as fh:
            mock.get(f'{DOMAINS_BASE}/2/records?page=1', text=fh.read())
        with open('tests/fixtures/linode-records-page-2.json') as fh:
            mock.get(f'{DOMAINS_BASE}/2/records?page=2', text=fh.read())

    def test_populate(self):
        provider = LinodeProvider('test', 'token')

        # Bad auth
        with requests_mock() as mock:
            mock.get(
                ANY,
                status_code=401,
                json={'errors': [{'reason': 'Invalid Token'}]},
            )

            with self.assertRaises(LinodeClientUnauthorized):
                zone = Zone('unit.tests.', [])
                provider.populate(zone)

        # General error
        with requests_mock() as mock:
            mock.get(ANY, status_code=502, text='Things caught fire')

            with self.assertRaises(HTTPError) as ctx:
                zone = Zone('unit.tests.', [])
                provider.populate(zone)
            self.assertEqual(502, ctx.exception.response.status_code)

        # Non-existent domain doesn't populate anything
        with requests_mock() as mock:
            mock.get(
                f'{DOMAINS_BASE}?page=1',
                json={'page': 1, 'pages': 1, 'results': 0, 'data': []},
            )

            zone = Zone('unit.tests.', [])
            exists = provider.populate(zone)
            self.assertEqual(set(), zone.records)
            self.assertFalse(exists)

        # Fresh provider/zone, populate for real and check what we got
        provider = LinodeProvider('test', 'token', strict_supports=False)
        with requests_mock() as mock:
            self._mock_zone(mock)

            zone = Zone('unit.tests.', [])
            exists = provider.populate(zone)
            self.assertTrue(exists)
            self.assertEqual(14, len(zone.records))

            # No diffs == no changes, this also exercises ttl-rounding,
            # CAA flag stripping, and root NS/unsupported-type handling in
            # _process_desired_zone since self.expected has all of those
            plan = provider.plan(self.expected)
            self.assertIsNone(plan)

        # 2nd populate makes no network calls/all from cache
        again = Zone('unit.tests.', [])
        provider.populate(again)
        self.assertEqual(14, len(again.records))

    def test_list_zones(self):
        provider = LinodeProvider('test', 'token')

        with requests_mock() as mock:
            with open('tests/fixtures/linode-domains-page-1.json') as fh:
                mock.get(f'{DOMAINS_BASE}?page=1', text=fh.read())
            with open('tests/fixtures/linode-domains-page-2.json') as fh:
                mock.get(f'{DOMAINS_BASE}?page=2', text=fh.read())

            self.assertEqual(
                ['other.com.', 'sub.unit.tests.', 'unit.tests.'],
                provider.list_zones(),
            )

    def test_process_desired_zone_srv_subdomain_unsupported(self):
        provider = LinodeProvider('test', 'token', strict_supports=False)

        def _zone_with_bad_srv():
            zone = Zone('unit.tests.', [])
            zone.add_record(
                Record.new(
                    zone,
                    '_sip._tcp.office',
                    {
                        'ttl': 600,
                        'type': 'SRV',
                        'values': [
                            {
                                'port': 5060,
                                'priority': 10,
                                'weight': 20,
                                'target': 'sip.unit.tests.',
                            }
                        ],
                    },
                )
            )
            return zone

        processed = provider._process_desired_zone(_zone_with_bad_srv())
        self.assertEqual(set(), processed.records)

        # and with strict_supports (the default) it raises instead of
        # silently dropping the record
        strict = LinodeProvider('test', 'token')
        with self.assertRaises(SupportsException):
            strict._process_desired_zone(_zone_with_bad_srv())

    def test_process_desired_zone_caa_flags_unsupported(self):
        provider = LinodeProvider('test', 'token', strict_supports=False)

        zone = Zone('unit.tests.', [])
        zone.add_record(
            Record.new(
                zone,
                '',
                {
                    'ttl': 3600,
                    'type': 'CAA',
                    'values': [
                        {'flags': 128, 'tag': 'issue', 'value': 'ca.unit.tests'}
                    ],
                },
            )
        )

        processed = provider._process_desired_zone(zone)
        record = list(processed.records)[0]
        self.assertEqual(0, record.values[0].flags)

    def test_process_desired_zone_ttl_rounding(self):
        provider = LinodeProvider('test', 'token', strict_supports=False)

        zone = Zone('unit.tests.', [])
        zone.add_record(
            Record.new(
                zone, 'www', {'ttl': 60, 'type': 'A', 'value': '1.2.3.4'}
            )
        )

        processed = provider._process_desired_zone(zone)
        record = list(processed.records)[0]
        self.assertEqual(300, record.ttl)

        # anything over the largest valid value clamps to it, rather than
        # falling off the end of VALID_TTLS
        zone = Zone('unit.tests.', [])
        zone.add_record(
            Record.new(
                zone, 'www', {'ttl': 99999999, 'type': 'A', 'value': '1.2.3.4'}
            )
        )
        processed = provider._process_desired_zone(zone)
        record = list(processed.records)[0]
        self.assertEqual(2419200, record.ttl)

    def test_populate_skips_unsupported_record_type(self):
        provider = LinodeProvider('test', 'token')
        provider._domains = {'unit.tests.': {'id': 1, 'domain': 'unit.tests'}}
        provider._client.records = Mock(
            return_value=[
                {
                    'id': 1,
                    'type': 'SOA',
                    'name': '',
                    'target': 'ignored',
                    'ttl_sec': 3600,
                }
            ]
        )

        zone = Zone('unit.tests.', [])
        exists = provider.populate(zone)
        self.assertTrue(exists)
        self.assertEqual(set(), zone.records)

    def test_populate_targets_with_trailing_dot_already(self):
        # Linode isn't consistent about whether it hands a target back
        # with a trailing dot still attached, live testing showed PTR
        # records keep it while CNAME/NS/MX don't. Make sure neither case
        # ends up double-dotted.
        provider = LinodeProvider('test', 'token')
        provider._domains = {'unit.tests.': {'id': 1, 'domain': 'unit.tests'}}
        provider._client.records = Mock(
            return_value=[
                {
                    'id': 1,
                    'type': 'PTR',
                    'name': 'ptr',
                    'target': 'host.unit.tests.',
                    'ttl_sec': 300,
                },
                {
                    'id': 2,
                    'type': 'CNAME',
                    'name': 'cname',
                    'target': 'unit.tests.',
                    'ttl_sec': 300,
                },
                {
                    'id': 3,
                    'type': 'NS',
                    'name': 'under',
                    'target': 'ns1.unit.tests.',
                    'ttl_sec': 3600,
                },
                {
                    'id': 4,
                    'type': 'MX',
                    'name': 'mx',
                    'target': 'mail.unit.tests.',
                    'priority': 10,
                    'ttl_sec': 300,
                },
                {
                    'id': 5,
                    'type': 'SRV',
                    'name': '',
                    'service': 'sip',
                    'protocol': 'tcp',
                    'target': 'sip.unit.tests.',
                    'priority': 10,
                    'weight': 20,
                    'port': 5060,
                    'ttl_sec': 3600,
                },
            ]
        )

        zone = Zone('unit.tests.', [])
        provider.populate(zone)
        by_name = {(r.name, r._type): r for r in zone.records}

        self.assertEqual('host.unit.tests.', by_name[('ptr', 'PTR')].value)
        self.assertEqual('unit.tests.', by_name[('cname', 'CNAME')].value)
        self.assertEqual(['ns1.unit.tests.'], by_name[('under', 'NS')].values)
        self.assertEqual(
            'mail.unit.tests.', by_name[('mx', 'MX')].values[0].exchange
        )
        self.assertEqual(
            'sip.unit.tests.', by_name[('_sip._tcp', 'SRV')].values[0].target
        )

    def test_apply(self):
        provider = LinodeProvider('test', 'token', strict_supports=False)

        resp = Mock()
        resp.json = Mock()
        provider._client._request = Mock(return_value=resp)

        # no matching domain, create everything
        resp.json.side_effect = [
            {'page': 1, 'pages': 1, 'results': 0, 'data': []},  # populate
            {'id': 42, 'domain': 'unit.tests', 'type': 'master'},  # create
        ]
        plan = provider.plan(self.expected)

        self.assertFalse(plan.exists)
        n = len(plan.changes)
        self.assertEqual(14, n)
        self.assertEqual(n, provider.apply(plan))

        request = provider._client._request
        # zone didn't exist, domain was created with a default soa_email
        self.assertEqual(
            call('GET', '/domains', {'page': 1}), request.mock_calls[0]
        )
        request.assert_any_call(
            'POST',
            '/domains',
            data={
                'domain': 'unit.tests',
                'type': 'master',
                'soa_email': 'hostmaster@unit.tests',
            },
        )
        # a simple multi-value record
        request.assert_any_call(
            'POST',
            '/domains/42/records',
            data={
                'type': 'A',
                'name': 'www',
                'target': '2.2.3.6',
                'ttl_sec': 300,
            },
        )
        # an SRV record, split in to service/protocol/target
        request.assert_any_call(
            'POST',
            '/domains/42/records',
            data={
                'type': 'SRV',
                'service': 'srv',
                'protocol': 'tcp',
                'target': 'foo-1.unit.tests.',
                'priority': 10,
                'weight': 20,
                'port': 30,
                'ttl_sec': 3600,
            },
        )
        # a CAA record, flags always 0
        request.assert_any_call(
            'POST',
            '/domains/42/records',
            data={
                'type': 'CAA',
                'name': '',
                'target': 'ca.unit.tests',
                'tag': 'issue',
                'ttl_sec': 3600,
            },
        )
        # 1 GET (domains) + 1 POST (domain create) + 22 POSTs (one per
        # value across the 14 created records)
        self.assertEqual(24, request.call_count)

        request.reset_mock()

        # delete one, update one
        provider._client.records = Mock(
            return_value=[
                {
                    'id': 101,
                    'type': 'A',
                    'name': 'www',
                    'target': '1.2.3.4',
                    'ttl_sec': 300,
                },
                {
                    'id': 102,
                    'type': 'A',
                    'name': 'ttl',
                    'target': '3.2.3.4',
                    'ttl_sec': 3600,
                },
            ]
        )

        wanted = Zone('unit.tests.', [])
        wanted.add_record(
            Record.new(
                wanted, 'ttl', {'ttl': 300, 'type': 'A', 'value': '3.2.3.4'}
            )
        )

        plan = provider.plan(wanted)
        self.assertTrue(plan.exists)
        self.assertEqual(2, len(plan.changes))
        self.assertEqual(2, provider.apply(plan))

        request.assert_has_calls(
            [
                call('DELETE', '/domains/42/records/101'),
                call('DELETE', '/domains/42/records/102'),
                call(
                    'POST',
                    '/domains/42/records',
                    data={
                        'type': 'A',
                        'name': 'ttl',
                        'target': '3.2.3.4',
                        'ttl_sec': 300,
                    },
                ),
            ],
            any_order=True,
        )

        request.reset_mock()

        # updating an SRV record deletes by service/protocol, since
        # Linode's "name" field is unused for them
        provider._client.records = Mock(
            return_value=[
                {
                    'id': 200,
                    'type': 'SRV',
                    'name': '',
                    'service': 'xmpp',
                    'protocol': 'tcp',
                    'target': 'other.unit.tests',
                    'priority': 10,
                    'weight': 20,
                    'port': 5222,
                    'ttl_sec': 3600,
                },
                {
                    'id': 201,
                    'type': 'SRV',
                    'name': '',
                    'service': 'sip',
                    'protocol': 'tcp',
                    'target': 'old.unit.tests',
                    'priority': 10,
                    'weight': 20,
                    'port': 5060,
                    'ttl_sec': 3600,
                },
            ]
        )

        wanted = Zone('unit.tests.', [])
        wanted.add_record(
            Record.new(
                wanted,
                '_sip._tcp',
                {
                    'ttl': 3600,
                    'type': 'SRV',
                    'values': [
                        {
                            'port': 5060,
                            'priority': 10,
                            'weight': 20,
                            'target': 'new.unit.tests.',
                        }
                    ],
                },
            )
        )

        plan = provider.plan(wanted)
        # _xmpp._tcp isn't wanted any more (delete) and _sip._tcp changed
        # (update), the latter exercises multi-record SRV delete matching
        self.assertEqual(2, len(plan.changes))
        self.assertEqual(2, provider.apply(plan))

        request.assert_has_calls(
            [
                call('DELETE', '/domains/42/records/200'),
                call('DELETE', '/domains/42/records/201'),
                call(
                    'POST',
                    '/domains/42/records',
                    data={
                        'type': 'SRV',
                        'service': 'sip',
                        'protocol': 'tcp',
                        'target': 'new.unit.tests.',
                        'priority': 10,
                        'weight': 20,
                        'port': 5060,
                        'ttl_sec': 3600,
                    },
                ),
            ],
            any_order=True,
        )
