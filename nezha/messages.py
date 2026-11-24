"""Protobuf encoders/decoders for the Nezha protocol messages.

Field numbers are taken verbatim from the official proto definition
(nezhahq/agent, proto/nezha.proto — a reference copy lives in ./nezha.proto).

  Host  { string platform=1; string platform_version=2; repeated string cpu=3;
          uint64 mem_total=4; uint64 disk_total=5; uint64 swap_total=6;
          string arch=7; string virtualization=8; uint64 boot_time=9;
          string version=10; repeated string gpu=11; }
  State { double cpu=1; uint64 mem_used=2; uint64 swap_used=3;
          uint64 disk_used=4; uint64 net_in_transfer=5;
          uint64 net_out_transfer=6; uint64 net_in_speed=7;
          uint64 net_out_speed=8; uint64 uptime=9; double load1=10;
          double load5=11; double load15=12; uint64 tcp_conn_count=13;
          uint64 udp_conn_count=14; uint64 process_count=15;
          repeated State_SensorTemperature temperatures=16;
          repeated double gpu=17; repeated State_GPU gpus=18; }
  Task       { uint64 id=1; uint64 type=2; string data=3; }
  TaskResult { uint64 id=1; uint64 type=2; float delay=3; string data=4;
               bool successful=5; }
  Receipt       { bool proced=1; }
  Uint64Receipt { uint64 data=1; }
  GeoIP { bool use6=1; IP ip=2; string country_code=3;
          uint64 dashboard_boot_time=4; }
  IP { string ipv4=1; string ipv6=2; }
"""
from . import protobuf as pb


def encode_host(h):
    w = pb.Writer()
    if h.get('platform'):
        w.string(1, h['platform'])
    if h.get('platform_version'):
        w.string(2, h['platform_version'])
    for c in h.get('cpu') or []:
        w.string(3, c)
    if h.get('mem_total'):
        w.varint(4, h['mem_total'])
    if h.get('disk_total'):
        w.varint(5, h['disk_total'])
    if h.get('swap_total'):
        w.varint(6, h['swap_total'])
    if h.get('arch'):
        w.string(7, h['arch'])
    if h.get('virtualization'):
        w.string(8, h['virtualization'])
    if h.get('boot_time'):
        w.varint(9, h['boot_time'])
    if h.get('version'):
        w.string(10, h['version'])
    for g in h.get('gpu') or []:
        w.string(11, g)
    return w.finish()


def encode_state(s):
    w = pb.Writer()
    if s.get('cpu'):
        w.double(1, s['cpu'])
    if s.get('mem_used'):
        w.varint(2, s['mem_used'])
    if s.get('swap_used'):
        w.varint(3, s['swap_used'])
    if s.get('disk_used'):
        w.varint(4, s['disk_used'])
    if s.get('net_in_transfer'):
        w.varint(5, s['net_in_transfer'])
    if s.get('net_out_transfer'):
        w.varint(6, s['net_out_transfer'])
    if s.get('net_in_speed'):
        w.varint(7, s['net_in_speed'])
    if s.get('net_out_speed'):
        w.varint(8, s['net_out_speed'])
    if s.get('uptime'):
        w.varint(9, s['uptime'])
    if s.get('load1'):
        w.double(10, s['load1'])
    if s.get('load5'):
        w.double(11, s['load5'])
    if s.get('load15'):
        w.double(12, s['load15'])
    if s.get('tcp_conn_count'):
        w.varint(13, s['tcp_conn_count'])
    if s.get('udp_conn_count'):
        w.varint(14, s['udp_conn_count'])
    if s.get('process_count'):
        w.varint(15, s['process_count'])
    # temperatures (16), gpu (17), gpus (18): not collected by this agent.
    return w.finish()


def decode_task(buf):
    p = pb.parse(buf)
    return {'id': pb.u64(p, 1), 'type': pb.u64(p, 2), 'data': pb.string(p, 3)}


def encode_task_result(r):
    w = pb.Writer()
    if r.get('id'):
        w.varint(1, r['id'])
    if r.get('type'):
        w.varint(2, r['type'])
    if r.get('delay'):
        w.float(3, r['delay'])
    if r.get('data'):
        w.string(4, r['data'])
    if r.get('successful'):
        w.boolean(5, True)
    return w.finish()


def decode_receipt(buf):
    p = pb.parse(buf)
    return {'proced': pb.boolean(p, 1)}


def decode_uint64_receipt(buf):
    p = pb.parse(buf)
    return {'data': pb.u64(p, 1)}


def encode_geoip(g):
    w = pb.Writer()
    if g.get('use6'):
        w.boolean(1, True)
    if g.get('ip'):
        ipw = pb.Writer()
        if g['ip'].get('ipv4'):
            ipw.string(1, g['ip']['ipv4'])
        if g['ip'].get('ipv6'):
            ipw.string(2, g['ip']['ipv6'])
        w.message(2, ipw.finish())
    if g.get('country_code'):
        w.string(3, g['country_code'])
    if g.get('dashboard_boot_time'):
        w.varint(4, g['dashboard_boot_time'])
    return w.finish()
