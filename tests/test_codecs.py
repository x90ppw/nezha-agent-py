"""Unit tests for the hand-rolled codecs: protobuf, messages, HPACK.

Run:  python3 -m unittest discover -s tests -v
HPACK vectors are the worked examples from RFC 7541 Appendix C.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nezha import hpack, messages, protobuf as pb
from nezha.grpc import FrameParser, frame_message


class TestProtobuf(unittest.TestCase):
    def test_varint_roundtrip(self):
        for value in (0, 1, 127, 128, 300, 2**32, 2**63 - 1, 2**64 - 1):
            w = pb.Writer()
            w.varint(1, value)
            parsed = pb.parse(w.finish())
            self.assertEqual(pb.u64(parsed, 1), value)

    def test_all_wire_types(self):
        w = pb.Writer()
        w.string(1, 'nezha')
        w.double(2, 3.25)
        w.float(3, 1.5)
        w.boolean(4, True)
        w.bytes(5, b'\x00\x01')
        p = pb.parse(w.finish())
        self.assertEqual(pb.string(p, 1), 'nezha')
        self.assertAlmostEqual(pb.first(p, 2).value, 3.25)
        self.assertAlmostEqual(pb.first(p, 3).value, 1.5)
        self.assertTrue(pb.boolean(p, 4))
        self.assertEqual(pb.first(p, 5).buffer, b'\x00\x01')


class TestMessages(unittest.TestCase):
    def test_host_roundtrip(self):
        host = {
            'platform': 'linux', 'platform_version': '6.8.0',
            'cpu': ['Test CPU (4 cores)'], 'mem_total': 8 << 30,
            'disk_total': 100 << 30, 'swap_total': 2 << 30,
            'arch': 'amd64', 'virtualization': '', 'boot_time': 1700000000,
            'version': '1.0.0', 'gpu': [],
        }
        p = pb.parse(messages.encode_host(host))
        self.assertEqual(pb.string(p, 1), 'linux')
        self.assertEqual(pb.string(p, 3), 'Test CPU (4 cores)')
        self.assertEqual(pb.u64(p, 4), 8 << 30)
        self.assertEqual(pb.u64(p, 9), 1700000000)
        self.assertEqual(pb.string(p, 10), '1.0.0')

    def test_state_roundtrip(self):
        state = {'cpu': 12.5, 'mem_used': 12345, 'uptime': 999,
                 'load1': 0.5, 'tcp_conn_count': 42}
        p = pb.parse(messages.encode_state(state))
        self.assertAlmostEqual(pb.first(p, 1).value, 12.5)
        self.assertEqual(pb.u64(p, 2), 12345)
        self.assertEqual(pb.u64(p, 9), 999)
        self.assertEqual(pb.u64(p, 13), 42)

    def test_task_decode_and_result_encode(self):
        w = pb.Writer()
        w.varint(1, 77)
        w.varint(2, 3)
        w.string(3, '127.0.0.1:8008')
        task = messages.decode_task(w.finish())
        self.assertEqual(task, {'id': 77, 'type': 3, 'data': '127.0.0.1:8008'})

        p = pb.parse(messages.encode_task_result(
            {'id': 77, 'type': 3, 'delay': 2.5, 'data': '', 'successful': True}))
        self.assertEqual(pb.u64(p, 1), 77)
        self.assertAlmostEqual(pb.first(p, 3).value, 2.5)
        self.assertTrue(pb.boolean(p, 5))

    def test_geoip_roundtrip(self):
        buf = messages.encode_geoip({
            'use6': False, 'ip': {'ipv4': '203.0.113.7'},
            'country_code': 'de', 'dashboard_boot_time': 0})
        p = pb.parse(buf)
        ip = pb.parse(pb.first(p, 2).buffer)
        self.assertEqual(pb.string(ip, 1), '203.0.113.7')
        self.assertEqual(pb.string(p, 3), 'de')


class TestHpack(unittest.TestCase):
    def test_rfc_c21_literal_with_indexing(self):
        dec = hpack.Decoder()
        headers = dec.decode(bytes.fromhex(
            '400a637573746f6d2d6b65790d637573746f6d2d686561646572'))
        self.assertEqual(headers, [('custom-key', 'custom-header')])
        self.assertEqual(dec.dynamic[0], ('custom-key', 'custom-header'))

    def test_rfc_c31_first_request_no_huffman(self):
        dec = hpack.Decoder()
        headers = dec.decode(bytes.fromhex(
            '828684410f7777772e6578616d706c652e636f6d'))
        self.assertEqual(headers, [
            (':method', 'GET'), (':scheme', 'http'), (':path', '/'),
            (':authority', 'www.example.com')])

    def test_rfc_c41_first_request_huffman(self):
        dec = hpack.Decoder()
        headers = dec.decode(bytes.fromhex(
            '828684418cf1e3c2e5f23a6ba0ab90f4ff'))
        self.assertEqual(headers, [
            (':method', 'GET'), (':scheme', 'http'), (':path', '/'),
            (':authority', 'www.example.com')])

    def test_rfc_c61_first_response_huffman(self):
        dec = hpack.Decoder()
        headers = dec.decode(bytes.fromhex(
            '488264025885aec3771a4b6196d07abe941054d444a8200595040b8166'
            'e082a62d1bff6e919d29ad171863c78f0b97c8e9ae82ae43d3'))
        self.assertEqual(headers, [
            (':status', '302'),
            ('cache-control', 'private'),
            ('date', 'Mon, 21 Oct 2013 20:13:21 GMT'),
            ('location', 'https://www.example.com')])

    def test_encoder_decoder_roundtrip(self):
        enc = hpack.Encoder()
        dec = hpack.Decoder()
        headers = [
            (':method', 'POST'),
            (':path', '/proto.NezhaService/ReportSystemState'),
            (':scheme', 'https'),
            (':authority', 'nezha.example.com:443'),
            ('content-type', 'application/grpc'),
            ('user-agent', 'nezha-agent-py/1.0.0'),
            ('te', 'trailers'),
            ('client-secret', 's3cret'),
            ('client-uuid', '123e4567-e89b-12d3-a456-426614174000'),
        ]
        self.assertEqual(dec.decode(enc.encode(headers)), headers)


class TestGrpcFraming(unittest.TestCase):
    def test_frame_parser(self):
        got = []
        parser = FrameParser(got.append)
        blob = frame_message(b'hello') + frame_message(b'')
        parser.push(blob[:4])          # split inside the header
        parser.push(blob[4:9])          # split inside the payload
        parser.push(blob[9:])
        self.assertEqual(got, [b'hello', b''])


if __name__ == '__main__':
    unittest.main()
