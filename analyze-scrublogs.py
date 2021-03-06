#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Analyze how your Ceph cluster performs (deep) scrubs
#
# Author:       Simon Leinen  <simon.leinen@switch.ch>
# Date created: 2015-04-07
#
# The input file should be generated using these commands:
#
#   ceph osd tree
#   ceph pg dump
#   foreach osd_host in $osd_hosts
#   do
#     ssh $osd_host zgrep scrub '/var/log/ceph/ceph-osd.*.{7,6,5,4,3,2,1}.gz'
#   done

from __future__ import print_function

import re
import sys
from datetime import datetime
from datetime import timedelta

__author_name__ = "Simon Leinen"
__author_email__ = "simon.leinen@switch.ch"
__author__ = "%(__author_name__)s <%(__author_email__)s>" % vars()

_copyright_year_begin = "2015"
__date__ = "2015-04-07"
_copyright_year_latest = __date__.split('-')[0]
_copyright_year_range = _copyright_year_begin
if _copyright_year_latest > _copyright_year_begin:
    _copyright_year_range += "?<80><93>%(_copyright_year_latest)s" % vars()
__copyright__ = (
    "Copyright © %(_copyright_year_range)s"
    " %(__author_name__)s") % vars()
__license__ = "GPL version 3"

LOG = '20150414-004026-scrub-logs.txt'

SCRUB_SHALLOW = 0
SCRUB_DEEP = 1


class ParseError(Exception):
    def __init__(self, msg):
        super(ParseError, self).__init__()
        self.msg = msg

    def __str__(self):
        return "Parse error: %s" % (self.msg)


class PG(object):

    """Ceph placement group"""

    def __init__(
            self,
            pgid,
            objects=0,
            bytes=0,
            up=None,
            acting=None,
            hosts=None):
        self.pgid = pgid
        self.objects = objects
        self.bytes = bytes
        if up is None:
            up = list()
        self.up = up
        if acting is None:
            acting = list()
        self.acting = acting
        if hosts is None:
            hosts = list()
        self.hosts = hosts

    def __str__(self):
        return "PG %6s (%6.2f GB) [%s] [%s]" \
            % (self.pgid,
               self.bytes * 1e-9,
               ",".join(self.hosts),
               ",".join([str(x) for x in self.acting]))


class EventLog(object):

    """A log containing events

    The event objects it stores require a "time" attribute that is
    used as an index.

    """

    def __init__(
            self):
        self.log = dict()

    def add(self, event):
        if event.time in self.log:
            self.log[event.time].append(event)
        else:
            self.log[event.time] = list([event])

    def forward(self):
        for time in sorted(self.log.keys()):
            ev = self.log[time]
            for ev in ev:
                yield ev


class Event(object):

    def __init__(self, time):
        self.time = time


class ScrubEvent(Event):

    """Represent a scrubbing event"""

    def __init__(self, time,
                 scrub_type, pg, start=0):
        super(ScrubEvent, self).__init__(time)
        self.scrub_type = scrub_type
        self.start = start
        self.pg = pg

    def __str__(self):
        start_end = '<' if self.start else '>'
        deep_shallow = 'S' if self.scrub_type == SCRUB_SHALLOW else 'D'
        return "%s %s%s %s" % (self.time, start_end, deep_shallow, self.pg)


class OSDSlowRequestEvent(Event):

    """Represent a "slow request" event signaled by an OSD"""

    def __init__(self, time, osdno, description):
        super(OSDSlowRequestEvent, self).__init__(time)
        self.osdno = osdno
        self.description = description

    def __str__(self):
        return "%s slow request OSD %d: %s" \
            % (self.time, self.osdno, self.description)


def parse_scrub_type(scrub_type):
    if scrub_type == 'scrub':
        return SCRUB_SHALLOW
    elif scrub_type == 'deep-scrub':
        return SCRUB_DEEP
    else:
        raise ParseError("Unknown scrub type %s" % (scrub_type))

TSTAMP_RE = r'(\d\d+-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d+)'

# Estimated scrubbing rate in bytes/sec
#
SCRUB_RATE_EST = 80e6


class CephScrubLogAnalyzer(object):

    """A tool to analyze Ceph scrubbing schedule from logs

    """
    def __init__(
            self,
            log,
            min_time=None,
            scrub_rate_est=SCRUB_RATE_EST,
            log_unknown_lines=False,
    ):
        self.log_file_name = log
        self.min_time = min_time
        self.scrub_rate_est = scrub_rate_est
        self.log_unknown_lines = log_unknown_lines

        self.scrub_count, self.shallow_count, self.deep_count = 0, 0, 0
        self.log = EventLog()
        self.osd_to_host = dict()
        self.osd_to_kb_used = dict()
        self.pg = dict()
        self.current_host = None

    def parse(self):
        def parse_osd_log_scrub_line(line, osdno, tstamp):
            if not hasattr(self, 'OSD_LOG_SCRUB_RE'):
                self.OSD_LOG_SCRUB_RE \
                    = re.compile('(.*) (deep-scrub|scrub) ok')
            match = self.OSD_LOG_SCRUB_RE.match(line)
            if match:
                pgid = match.group(1)
                scrub_type = parse_scrub_type(match.group(2))
                if scrub_type == SCRUB_SHALLOW:
                    self.shallow_count = self.shallow_count+1
                elif scrub_type == SCRUB_DEEP:
                    self.deep_count = self.deep_count+1
                    pg = self.pg[pgid]
                    self.log.add(ScrubEvent(tstamp,
                                            scrub_type=scrub_type,
                                            pg=pg))
                self.scrub_count = self.scrub_count+1
                return True
            return False

        def parse_osd_log_slow_line(line, osdno, tstamp):

            def parse_slow_osd_op(msg):
                if not hasattr(self, 'OSD_SLOW_OSD_OP_RE'):
                    self.OSD_SLOW_OSD_OP_RE \
                        = re.compile(r'osd_op\((.*)\) ' +
                                     'v4 currently ' +
                                     '(waiting for ' +
                                     '(subops from ([0-9,]+)' +
                                     '|scrub' +
                                     '|degraded object' +
                                     ')' +
                                     '|started|reached pg' +
                                     '|no flag points reached' +
                                     '|commit sent)')
                match = self.OSD_SLOW_OSD_OP_RE.match(msg)
                if match:
                    self.log.add(OSDSlowRequestEvent(tstamp, osdno, msg))
                    return True
                return False

            def parse_slow_osd_sub_op(msg):
                if not hasattr(self, 'OSD_SLOW_OSD_SUB_OP_RE'):
                    self.OSD_SLOW_OSD_SUB_OP_RE \
                        = re.compile(r'osd_sub_op\((.*)\) ' +
                                     'v11 currently ' +
                                     '(commit sent' +
                                     '|no flag points reached|started)')
                match = self.OSD_SLOW_OSD_SUB_OP_RE.match(msg)
                if match:
                    self.log.add(OSDSlowRequestEvent(tstamp, osdno, msg))
                    return True
                return False

            def parse_slow_osd_sub_op_reply(msg):
                if not hasattr(self, 'OSD_SLOW_OSD_SUB_OP_REPLY_RE'):
                    self.OSD_SLOW_OSD_SUB_OP_REPLY_RE \
                        = re.compile(r'osd_sub_op_reply\((.*)\) ' +
                                     'v2 currently ' +
                                     '(no flag points reached)')
                match = self.OSD_SLOW_OSD_SUB_OP_REPLY_RE.match(msg)
                if match:
                    self.log.add(OSDSlowRequestEvent(tstamp, osdno, msg))
                    return True
                return False

            if not hasattr(self, 'OSD_LOG_SLOW_RE'):
                self.OSD_LOG_SLOW_RE \
                    = re.compile('slow request ([0-9.]+) seconds old, ' +
                                 'received at ' + TSTAMP_RE + ': (.*)')
            match = self.OSD_LOG_SLOW_RE.match(line)
            if match:
                age = float(match.group(1))
                received = parse_timestamp(match.group(2), match.group(3))
                explanation = match.group(4)
                if parse_slow_osd_op(explanation):
                    pass
                elif parse_slow_osd_sub_op(explanation):
                    pass
                elif parse_slow_osd_sub_op_reply(explanation):
                    pass
                else:
                    print("%s Slow request OSD %d [%s], age %5.2fs: %s"
                          % (received, osdno, osd_host(osdno),
                             age, explanation))
                return True
            return False

        def parse_osd_log_slows_line(line, osdno, tstamp):
            if not hasattr(self, 'OSD_LOG_SLOWS_RE'):
                self.OSD_LOG_SLOWS_RE \
                    = re.compile(r'(\d+) slow requests, ' +
                                 r'(\d+) included below; ' +
                                 'oldest blocked for > ([0-9.]+) secs')
            match = self.OSD_LOG_SLOWS_RE.match(line)
            if match:
                return True
            return False

        def parse_osd_param_set_line(line, osdno, tstamp):
            if not hasattr(self, 'OSD_PARAM_SET_RE'):
                self.OSD_PARAM_SET_RE \
                    = re.compile("^osd_scrub_sleep = '(.*)' ?$")
            if self.OSD_PARAM_SET_RE.match(line):
                return True
            return False

        def parse_osd_log_line(line):
            if not hasattr(self, 'OSD_LOG_RE'):
                self.OSD_LOG_RE \
                    = re.compile(r'^ */.*/ceph-osd\.(\d+)\.log' +
                                 r'(\.\d+(\.gz)?)?:' + TSTAMP_RE + r'\s+' +
                                 r'([0-9a-f]+)\s+0 log \[(.*)\] : (.*)}?$')
            match = self.OSD_LOG_RE.match(line)
            if not match:
                return False
            osdno = int(match.group(1))
            tstamp = parse_timestamp(match.group(4), match.group(5))
            if not self.min_time or self.min_time < tstamp:
                severity = match.group(7)
                rest = match.group(8)
                if parse_osd_log_scrub_line(rest, osdno, tstamp):
                    pass
                elif parse_osd_log_slow_line(rest, osdno, tstamp):
                    pass
                elif parse_osd_log_slows_line(rest, osdno, tstamp):
                    pass
                elif parse_osd_param_set_line(rest, osdno, tstamp):
                    pass
                else:
                    raise ParseError("Unrecognized OSD log line: \"%s\""
                                     % (line))
            return True

        def parse_timestamp(ymdhms, usec):
            return datetime.strptime(ymdhms, "%Y-%m-%d %H:%M:%S") \
                + timedelta(microseconds=int(usec))

        def parse_osd_set(s):
            # split()ting an empty string does not return the empty
            # list, but a list with a single empty string.  We have to
            # process this case separately.
            if s == '':
                return []
            return [int(x) for x in s.split(",")]

        def osd_host(osd):
            return self.osd_to_host[osd]

        def parse_pg(line):

            if not hasattr(self, 'PG_RE'):
                self.PG_RE \
                    = re.compile(r'^([0-9a-f]+\.[0-9a-f]+)\t\d+\t\d+\t\d+\t' +
                                 r'(\d+)\t(\d+)\t\d+\t\d+\t(\S+)\t' +
                                 TSTAMP_RE + r'\t\d+' + "'" +
                                 r'\d+\t\d+:\d+\t\[([0-9,]+)\]\t' +
                                 r'(\d+)\t\[([0-9,]+)\]\t(\d+)\t\d+' + "'" +
                                 r'\d+\t' + TSTAMP_RE + r'\t\d+' + "'" +
                                 r'\d+\t' + TSTAMP_RE + '$')
            match = self.PG_RE.match(line)
            if not match:
                return False
            pgid = match.group(1)
            objects = int(match.group(2))
            bytes = int(match.group(3))
            status = match.group(6)
            up_set = parse_osd_set(match.group(7))
            up_primary = int(match.group(8))
            assert up_set[0] == up_primary
            acting_set = parse_osd_set(match.group(9))
            acting_primary = int(match.group(10))
            assert acting_set[0] == acting_primary
            hosts = [osd_host(x) for x in acting_set]
            self.pg[pgid] = PG(pgid, objects=objects, bytes=bytes,
                               up=up_set, acting=acting_set,
                               hosts=hosts)
            return True

        def parse_osd_tree_host(line):
            if not hasattr(self, 'OSD_TREE_HOST_RE'):
                self.OSD_TREE_HOST_RE \
                    = re.compile(r'^(-\d+)\t(\d+\.\d+)\t\thost (.*)$')
            match = self.OSD_TREE_HOST_RE.match(line)
            if not match:
                return False
            self.current_host = match.group(3)
            return True

        def parse_osd_tree_osd(line):
            if not hasattr(self, 'OSD_TREE_OSD_RE'):
                self.OSD_TREE_OSD_RE \
                    = re.compile(r'^(\d+)\t(\d+\.\d+)\t\t\t' +
                                 r'osd\.(\d+)\tup\t1\t$')
            match = self.OSD_TREE_OSD_RE.match(line)
            if not match:
                return False
            osdno = int(match.group(1))
            self.osd_to_host[osdno] = self.current_host
            return True

        def parse_osd_stats(line):
            if not hasattr(self, 'OSD_STATS_RE'):
                self.OSD_STATS_RE \
                    = re.compile(r'^(\d+)\t(\d+)\t(\d+)\t(\d+)\t' +
                                 r'\[([0-9,]*)\]\t\[([0-9,]*)\]$')
            match = self.OSD_STATS_RE.match(line)
            if not match:
                return False
            osdno = int(match.group(1))
            kb_used = int(match.group(2))
            self.osd_to_kb_used[osdno] = kb_used
            return True

        for line in open(self.log_file_name):
            if parse_osd_log_line(line):
                pass
            elif parse_pg(line):
                pass
            elif parse_osd_tree_host(line):
                pass
            elif parse_osd_tree_osd(line):
                pass
            elif parse_osd_stats(line):
                pass
            elif self.log_unknown_lines:
                sys.stdout.write("?? "+line)
        print("Found %d scrubs, %d deep" % (self.scrub_count, self.deep_count))
        self.add_scrub_start_events()
        for event in self.log.forward():
            print(event)

    def add_scrub_start_event(self, end_event):

        def est_scrub_duration(size):
            scrub_init_duration = 1
            usec = size/(self.scrub_rate_est/1e6)+scrub_init_duration
            return timedelta(microseconds=usec)
        if end_event.scrub_type == SCRUB_DEEP:
            pg_size = end_event.pg.bytes
            est_start = end_event.time-est_scrub_duration(pg_size)
            self.log.add(ScrubEvent(est_start,
                                    scrub_type=end_event.scrub_type,
                                    start=True,
                                    pg=end_event.pg))

    def add_scrub_start_events(self):
        for event in self.log.forward():
            if isinstance(event, ScrubEvent):
                self.add_scrub_start_event(event)

ana = CephScrubLogAnalyzer(log=LOG,
                           min_time=datetime(2015, 04, 01))
ana.parse()
