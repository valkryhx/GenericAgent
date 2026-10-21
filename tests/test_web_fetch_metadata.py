import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.metadata import (  # noqa: E402
    check_metadata_addresses,
    metadata_guard_enabled,
)
from genericagent.tools.web_fetch.models import FetchError  # noqa: E402


class MetadataGuardTest(unittest.TestCase):
    def test_metadata_ip_is_rejected(self):
        def resolver(host, port, type):
            return [(2, 1, 6, "", ("169.254.169.254", 0))]

        with self.assertRaises(FetchError) as raised:
            check_metadata_addresses("evil.example", resolver=resolver)

        self.assertEqual("METADATA_ADDRESS_BLOCKED", raised.exception.code)

    def test_ordinary_private_address_is_not_rejected_by_a_route(self):
        def resolver(host, port, type):
            return [(2, 1, 6, "", ("10.0.0.5", 0))]

        addresses = check_metadata_addresses("internal.example", resolver=resolver)

        self.assertEqual(("10.0.0.5",), addresses)

    def test_disable_switch_skips_resolution(self):
        def resolver(host, port, type):
            raise AssertionError("must not resolve when disabled")

        self.assertEqual(
            (),
            check_metadata_addresses("metadata.example", enabled=False, resolver=resolver),
        )
        self.assertFalse(metadata_guard_enabled({"GA_WEBFETCH_BLOCK_METADATA_IPS": "0"}))

    def test_resolution_failure_is_a_transport_error(self):
        def resolver(host, port, type):
            raise OSError("dns down")

        with self.assertRaises(FetchError) as raised:
            check_metadata_addresses("example.com", resolver=resolver)

        self.assertEqual("EGRESS_UNAVAILABLE", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
