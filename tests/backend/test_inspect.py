from __future__ import annotations

import unittest

from gentoo_installerd.inspect import Discovery, Disk, find_disk, parse_lsblk

from tests.backend.helpers import FakeRunner, lsblk


class DiscoveryTests(unittest.TestCase):
    def test_lsblk_json_is_parsed_recursively(self) -> None:
        disks = parse_lsblk(lsblk())
        self.assertEqual(disks[0].path, "/dev/sda")
        self.assertEqual(disks[0].children[0].size, 1000000)
        self.assertEqual([node.path for node in disks[0].walk()], ["/dev/sda", "/dev/sda1"])

    def test_malformed_lsblk_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_lsblk("{}")

    def test_find_disk_requires_unique_node(self) -> None:
        disks = parse_lsblk(lsblk())
        self.assertEqual(find_disk(disks, "/dev/sda").serial, "disk-serial")
        with self.assertRaises(ValueError):
            find_disk(disks, "/dev/missing")

    def test_discovery_uses_argument_array(self) -> None:
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        disks = Discovery(runner).disks()
        self.assertEqual(runner.commands[0][:3], ("lsblk", "--json", "--bytes"))
        self.assertEqual(len(disks), 1)


if __name__ == "__main__":
    unittest.main()
