## Linode provider for octoDNS

An [octoDNS](https://github.com/octodns/octodns/) provider that targets [Linode](https://www.linode.com/docs/api/domains/)'s DNS Manager.

### Installation

#### Command line

```
pip install octodns-linode
```

#### requirements.txt/setup.py

Pinning specific versions or SHAs is recommended to avoid unplanned upgrades.

##### Versions

```
# Start with the latest versions and don't just copy what's here
octodns==0.9.14
octodns-linode==0.0.1
```

##### SHAs

```
# Start with the latest/specific versions and don't just copy what's here
-e git+https://git@github.com/octodns/octodns.git@9da19749e28f68407a1c246dfdf65663cdc1c422#egg=octodns
-e git+https://git@github.com/octodns/octodns-linode.git@ec9661f8b335241ae4746eea467a8509205e6a30#egg=octodns_linode
```

### Configuration

```yaml
providers:
  linode:
    class: octodns_linode.LinodeProvider
    # A Linode API v4 personal access token with read/write access to
    # Domains, https://cloud.linode.com/profile/tokens
    token: env/LINODE_TOKEN
    # The SOA email address to use when a zone doesn't already exist and
    # has to be created. Optional, defaults to hostmaster@<zone>.
    # soa_email: hostmaster@example.com
```

### Support Information

#### Records

LinodeProvider supports A, AAAA, CAA, CNAME, MX, NS, PTR, SRV, and TXT records. Root/apex NS records are not supported since Linode manages a domain's own nameservers itself and doesn't expose them as editable records.

A few of Linode's API limitations are worked around, with a warning logged (or an exception raised, if `strict_supports` is enabled) whenever they kick in:

* **TTLs**: Linode only accepts a specific, discrete set of `ttl_sec` values (300, 3600, 7200, 14400, 28800, 57600, 86400, 172800, 345600, 604800, 1209600, and 2419200) and rounds anything else up. To keep octoDNS from perpetually "fixing" a TTL that Linode is just going to round anyway, the provider rounds up before planning.
* **CAA flags**: Linode's API has no `flags` field for CAA records, so any non-zero `flags` value is dropped and reported back as `0`.
* **SRV subdomains**: Linode only supports SRV records of the exact form `_service._protocol.<domain>`; it has no field for an additional subdomain component in the owner name (e.g. `_sip._tcp.office.example.com` isn't representable). Records like that are omitted.

#### Dynamic

LinodeProvider does not support dynamic records.

### Development

See the [/script/](/script/) directory for some tools to help with the development process. They generally follow the [Script to rule them all](https://github.com/github/scripts-to-rule-them-all) pattern. Most useful is `./script/bootstrap` which will create a venv and install both the runtime and development related requirements. It will also hook up a pre-commit hook that covers most of what's run by CI.
