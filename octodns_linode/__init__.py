#
#
#

import logging
from collections import defaultdict

from requests import Session

from octodns import __VERSION__ as octodns_version
from octodns.provider import ProviderException, SupportsException
from octodns.provider.base import BaseProvider
from octodns.record import Record

# TODO: remove __VERSION__ with the next major version release
__version__ = __VERSION__ = '0.0.1'


class LinodeClientException(ProviderException):
    pass


class LinodeClientUnauthorized(LinodeClientException):
    def __init__(self):
        super().__init__('Unauthorized')


class LinodeClient(object):
    BASE = 'https://api.linode.com/v4'

    def __init__(self, token):
        sess = Session()
        sess.headers.update(
            {
                'Authorization': f'Bearer {token}',
                'User-Agent': f'octodns/{octodns_version} octodns-linode/{__VERSION__}',
            }
        )
        self._sess = sess

    def _request(self, method, path, params=None, data=None):
        url = f'{self.BASE}{path}'
        resp = self._sess.request(method, url, params=params, json=data)
        if resp.status_code == 401:
            raise LinodeClientUnauthorized()
        resp.raise_for_status()
        return resp

    def _paginated(self, path):
        ret = []

        page = 1
        while True:
            data = self._request('GET', path, {'page': page}).json()

            ret += data['data']

            if page >= data['pages']:
                break
            page += 1

        return ret

    def domains(self):
        return self._paginated('/domains')

    def domain_create(self, name, soa_email):
        return self._request(
            'POST',
            '/domains',
            data={'domain': name, 'type': 'master', 'soa_email': soa_email},
        ).json()

    def records(self, domain_id):
        return self._paginated(f'/domains/{domain_id}/records')

    def record_create(self, domain_id, params):
        self._request('POST', f'/domains/{domain_id}/records', data=params)

    def record_delete(self, domain_id, record_id):
        self._request('DELETE', f'/domains/{domain_id}/records/{record_id}')


class LinodeProvider(BaseProvider):
    SUPPORTS_GEO = False
    SUPPORTS_DYNAMIC = False
    SUPPORTS_ROOT_NS = False
    SUPPORTS = set(
        ('A', 'AAAA', 'CAA', 'CNAME', 'MX', 'NS', 'PTR', 'SRV', 'TXT')
    )

    # Linode rounds any record ttl_sec that isn't one of these up to the
    # nearest value, https://techdocs.akamai.com/linode-api/reference/post-domain-record
    VALID_TTLS = (
        300,
        3600,
        7200,
        14400,
        28800,
        57600,
        86400,
        172800,
        345600,
        604800,
        1209600,
        2419200,
    )

    def __init__(self, id, token, soa_email=None, *args, **kwargs):
        self.log = logging.getLogger(f'LinodeProvider[{id}]')
        self.log.debug('__init__: id=%s, token=***', id)
        super().__init__(id, *args, **kwargs)
        self._client = LinodeClient(token)
        self._soa_email = soa_email
        self._domains = None
        self._zone_records = {}

    def _normalize_ttl(self, ttl):
        for valid in self.VALID_TTLS:
            if ttl <= valid:
                return valid
        return self.VALID_TTLS[-1]

    def _srv_service_protocol(self, record):
        # API has no field for a subdomain component of an SRV record's
        # owner name, records are only ever created as
        # _service._protocol.<domain>, so anything else can't be
        # represented, https://techdocs.akamai.com/linode-api/reference/post-domain-record
        parts = record.name.split('.')
        if (
            len(parts) != 2
            or not parts[0].startswith('_')
            or not parts[1].startswith('_')
        ):
            raise SupportsException(
                f'{self.id}: Linode only supports SRV records of the '
                f'form _service._protocol, additional subdomain labels '
                f'are not supported, {record.fqdn}'
            )
        return parts[0][1:], parts[1][1:]

    def _process_desired_zone(self, desired):
        for record in desired.records:
            if record._type == 'SRV':
                try:
                    self._srv_service_protocol(record)
                except SupportsException as e:
                    self.supports_warn_or_except(str(e), 'omitting record')
                    desired.remove_record(record)
                    continue

            if record._type == 'CAA' and any(v.flags for v in record.values):
                msg = f'CAA "flags" are not supported by Linode, {record.fqdn}'
                fallback = 'they will be set to 0'
                self.supports_warn_or_except(msg, fallback)
                record = record.copy()
                for value in record.values:
                    value.flags = 0
                desired.add_record(record, replace=True)

            ttl = record.ttl
            normalized = self._normalize_ttl(ttl)
            if normalized != ttl:
                msg = f'ttl {ttl} is not supported by Linode, {record.fqdn}'
                fallback = f'rounding up to {normalized}'
                self.supports_warn_or_except(msg, fallback)
                record = record.copy()
                record.ttl = normalized
                desired.add_record(record, replace=True)

        return super()._process_desired_zone(desired)

    def _zones(self):
        if self._domains is None:
            self._domains = {
                f'{d["domain"]}.': d for d in self._client.domains()
            }
        return self._domains

    def _domain_for_zone(self, zone):
        return self._zones().get(zone.name)

    def _domain_records(self, zone):
        if zone.name not in self._zone_records:
            domain = self._domain_for_zone(zone)
            if domain is None:
                return None
            self._zone_records[zone.name] = self._client.records(domain['id'])

        return self._zone_records[zone.name]

    def list_zones(self):
        self.log.debug('list_zones:')
        return sorted(self._zones().keys())

    @staticmethod
    def _fqdn(value):
        # API is inconsistent about whether a target comes back with its
        # trailing dot still attached, so only add one if it's missing
        # rather than assuming either way
        return value if value.endswith('.') else f'{value}.'

    def _data_for_multiple(self, _type, records):
        return {
            'ttl': records[0]['ttl_sec'],
            'type': _type,
            'values': [r['target'] for r in records],
        }

    _data_for_A = _data_for_multiple
    _data_for_AAAA = _data_for_multiple

    def _data_for_NS(self, _type, records):
        return {
            'ttl': records[0]['ttl_sec'],
            'type': _type,
            'values': [self._fqdn(r['target']) for r in records],
        }

    def _data_for_CAA(self, _type, records):
        # Linode has no "flags" field for CAA records, so it's always 0
        values = [
            {'flags': 0, 'tag': r['tag'], 'value': r['target']} for r in records
        ]
        return {'ttl': records[0]['ttl_sec'], 'type': _type, 'values': values}

    def _data_for_single(self, _type, records):
        record = records[0]
        return {
            'ttl': record['ttl_sec'],
            'type': _type,
            'value': self._fqdn(record['target']),
        }

    _data_for_CNAME = _data_for_single
    _data_for_PTR = _data_for_single

    def _data_for_MX(self, _type, records):
        values = [
            {'preference': r['priority'], 'exchange': self._fqdn(r['target'])}
            for r in records
        ]
        return {'ttl': records[0]['ttl_sec'], 'type': _type, 'values': values}

    def _data_for_SRV(self, _type, records):
        values = []
        for r in records:
            values.append(
                {
                    'port': r['port'],
                    'priority': r['priority'],
                    'weight': r['weight'],
                    'target': self._fqdn(r['target']),
                }
            )
        return {'ttl': records[0]['ttl_sec'], 'type': _type, 'values': values}

    def _data_for_TXT(self, _type, records):
        values = [r['target'].replace(';', '\\;') for r in records]
        return {'ttl': records[0]['ttl_sec'], 'type': _type, 'values': values}

    def populate(self, zone, target=False, lenient=False):
        self.log.debug(
            'populate: name=%s, target=%s, lenient=%s',
            zone.name,
            target,
            lenient,
        )

        records = self._domain_records(zone)
        exists = records is not None

        values = defaultdict(lambda: defaultdict(list))
        for record in records or []:
            _type = record['type']
            if _type not in self.SUPPORTS:
                self.log.warning(
                    'populate: skipping unsupported %s record', _type
                )
                continue
            name = record['name']
            if _type == 'SRV':
                # "name" is unused for SRV records, the owner
                # name is entirely encoded in service/protocol instead
                service = record['service'].lstrip('_')
                protocol = record['protocol'].lstrip('_')
                name = f'_{service}._{protocol}'
            values[name][_type].append(record)

        before = len(zone.records)
        for name, types in values.items():
            for _type, type_records in types.items():
                data_for = getattr(self, f'_data_for_{_type}')
                record = Record.new(
                    zone,
                    name,
                    data_for(_type, type_records),
                    source=self,
                    lenient=lenient,
                )
                zone.add_record(record, lenient=lenient)

        self.log.info(
            'populate:   found %s records, exists=%s',
            len(zone.records) - before,
            exists,
        )
        return exists

    def _params_for_multiple(self, record):
        for value in record.values:
            yield {
                'type': record._type,
                'name': record.name,
                'target': value,
                'ttl_sec': record.ttl,
            }

    _params_for_A = _params_for_multiple
    _params_for_AAAA = _params_for_multiple
    _params_for_NS = _params_for_multiple

    def _params_for_CAA(self, record):
        for value in record.values:
            yield {
                'type': record._type,
                'name': record.name,
                'target': value.value,
                'tag': value.tag,
                'ttl_sec': record.ttl,
            }

    def _params_for_single(self, record):
        yield {
            'type': record._type,
            'name': record.name,
            'target': record.value,
            'ttl_sec': record.ttl,
        }

    _params_for_CNAME = _params_for_single
    _params_for_PTR = _params_for_single

    def _params_for_MX(self, record):
        for value in record.values:
            yield {
                'type': record._type,
                'name': record.name,
                'target': value.exchange,
                'priority': value.preference,
                'ttl_sec': record.ttl,
            }

    def _params_for_SRV(self, record):
        service, protocol = self._srv_service_protocol(record)
        for value in record.values:
            yield {
                'type': record._type,
                'service': service,
                'protocol': protocol,
                'target': value.target,
                'priority': value.priority,
                'weight': value.weight,
                'port': value.port,
                'ttl_sec': record.ttl,
            }

    def _params_for_TXT(self, record):
        # API doesn't want things escaped in values so we have to strip
        # them here and add them when going the other way
        for value in record.values:
            yield {
                'type': record._type,
                'name': record.name,
                'target': value.replace('\\;', ';'),
                'ttl_sec': record.ttl,
            }

    def _apply_Create(self, change):
        new = change.new
        domain = self._domain_for_zone(new.zone)
        params_for = getattr(self, f'_params_for_{new._type}')
        for params in params_for(new):
            self._client.record_create(domain['id'], params)

    def _apply_Update(self, change):
        self._apply_Delete(change)
        self._apply_Create(change)

    def _apply_Delete(self, change):
        existing = change.existing
        zone = existing.zone
        domain = self._domain_for_zone(zone)

        if existing._type == 'SRV':
            service, protocol = self._srv_service_protocol(existing)
            for record in self._domain_records(zone):
                if (
                    record['type'] == 'SRV'
                    and record['service'].lstrip('_') == service
                    and record['protocol'].lstrip('_') == protocol
                ):
                    self._client.record_delete(domain['id'], record['id'])
        else:
            for record in self._domain_records(zone):
                if (
                    record['name'] == existing.name
                    and record['type'] == existing._type
                ):
                    self._client.record_delete(domain['id'], record['id'])

    def _apply(self, plan):
        desired = plan.desired
        changes = plan.changes
        self.log.debug(
            '_apply: zone=%s, len(changes)=%d', desired.name, len(changes)
        )

        domain = self._domain_for_zone(desired)
        if domain is None:
            self.log.debug('_apply:   no matching domain, creating')
            domain_name = desired.name[:-1]
            soa_email = self._soa_email or f'hostmaster@{domain_name}'
            domain = self._client.domain_create(domain_name, soa_email)
            self._zones()[desired.name] = domain

        for change in changes:
            class_name = change.__class__.__name__
            getattr(self, f'_apply_{class_name}')(change)

        # Clear out the cache, it's no longer valid after changes
        self._zone_records.pop(desired.name, None)
