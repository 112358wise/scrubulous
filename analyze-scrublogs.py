#!/usr/bin/env python
# -*- coding: utf-8 -*-
##
## Analyze how your Ceph cluster performs (deep) scrubs
##
## Author:       Simon Leinen  <simon.leinen@switch.ch>
## Date created: 2015-04-07
##
## The input file should be generated using these commands:
##
##   ceph osd tree
##   ceph pg dump
##   foreach osd_host in $osd_hosts
##   do
##     ssh $osd_host zgrep scrub '/var/log/ceph/ceph-osd.*.{7,6,5,4,3,2,1}.gz'
##   done

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

import os
import re
import sys
from datetime import datetime
from datetime import timedelta

LOG = '20150407-scrub-logs.txt'

SCRUB_SHALLOW = 0
SCRUB_DEEP = 1

class ParseError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return "Parse error: %s" % (self.msg)
    pass

class PG:

    """Ceph placement group"""

    def __init__(
            self,
            pgid,
            bytes=0,
            up=[],
            acting=[],
            hosts=[]):
        self.pgid = pgid
        self.bytes = bytes
        self.up = up
        self.acting = acting
        self.hosts = hosts

    def __str__(self):
        return "PG %6s (%6.2f GB) [%s] [%s]" \
            % (self.pgid,
               self.bytes * 1e-9,
               ",".join(self.hosts),
               ",".join(map(str,self.acting)))

class Event:

    def __init__(self, time):
        self.time=time

class ScrubEvent(Event):

    """Represent a scrubbing event"""

    def __init__(self, time,
                 scrub_type, pg, start=0):
        self.time=time
        self.scrub_type=scrub_type
        self.start=start
        self.pg=pg

    def __str__(self):
        start_end = '<' if self.start else '>'
        deep_shallow = 'S' if self.scrub_type == SCRUB_SHALLOW else 'D'
        return "%s %s%s %s" % (self.time, start_end, deep_shallow, self.pg)

def parse_scrub_type(scrub_type):
    if scrub_type == 'scrub':
        return SCRUB_SHALLOW
    elif scrub_type == 'deep-scrub':
        return SCRUB_DEEP
    else:
        raise ParseError("Unknown scrub type %s" % (scrub_type))

TSTAMP_RE = '(\d\d+-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d+)'

## Estimated scrubbing rate in bytes/sec
##
SCRUB_RATE_EST = 200e6

class CephScrubLogAnalyzer:

    def __init__(
            self,
            log,
            min_time=None,
            scrub_rate_est=SCRUB_RATE_EST,
            log_unknown_lines=False,
    ):
        self.log_file_name=log
        self.min_time=min_time
        self.scrub_rate_est=scrub_rate_est
        self.log_unknown_lines=log_unknown_lines

    def parse(self):
        self.OSD_LOG_RE = re.compile('^ */var/log/ceph/ceph-osd\.(\d+)\.log(\.\d+(\.gz)?)?:'
                                     +TSTAMP_RE+'\s+'
                                     +'([0-9a-f]+)\s+0 log \[(.*)\] : (.*)}?$')
        self.OSD_LOG_SCRUB_RE = re.compile('(.*) (deep-scrub|scrub) ok')
        self.OSD_LOG_SLOW_RE = re.compile('slow request ([0-9.]+) seconds old, '
                                          +'received at '+TSTAMP_RE
                                          +': (.*)')
        self.OSD_TREE_HOST_RE = re.compile('^(-\d+)\t(\d+\.\d+)\t\thost (.*)$')
        self.OSD_TREE_OSD_RE = re.compile('^(\d+)\t(\d+\.\d+)\t\t\t'
                                          +'osd\.(\d+)\tup\t1\t$')
        self.OSD_STATS_RE = re.compile('^(\d+)\t(\d+)\t(\d+)\t(\d+)\t\[([0-9,]*)\]\t\[([0-9,]*)\]$')
        self.PG_RE = re.compile('^([0-9a-f]+\.[0-9a-f]+)\t\d+\t\d+\t\d+\t'
                                +'\d+\t(\d+)\t\d+\t\d+\t(\S+)\t'
                                +TSTAMP_RE+'\t'
                                +'\d+'+"'"+'\d+\t\d+:\d+\t\[([0-9,]+)\]\t'
                                +'(\d+)\t\[([0-9,]+)\]\t(\d+)\t'
                                +'\d+'+"'"+'\d+\t'
                                +TSTAMP_RE+'\t'
                                +'\d+'+"'"+'\d+\t'
                                +TSTAMP_RE+'$'
        )

        def parse_osd_log_scrub_line(line, osdno, tstamp):
            m = self.OSD_LOG_SCRUB_RE.match(line)
            if m:
                pgid=m.group(1)
                scrub_type = parse_scrub_type(m.group(2))
                if scrub_type == SCRUB_SHALLOW:
                    self.shallow_count = self.shallow_count+1
                elif scrub_type == SCRUB_DEEP:
                    self.deep_count = self.deep_count+1
                    pg = self.pg[pgid]
                    self.log[tstamp] \
                        = ScrubEvent(tstamp, scrub_type=scrub_type, pg=pg)
                self.scrub_count = self.scrub_count+1
                return True
            return False

        def parse_osd_log_slow_line(line, osdno, tstamp):
            m = self.OSD_LOG_SLOW_RE.match(line)
            if m:
                age = float(m.group(1))
                received = parse_timestamp(m.group(2), m.group(3))
                explanation = m.group(4)
                print("%s Slow request OSD %d [%s], age %5.2fs: %s"
                      % (received, osdno, osd_host(osdno), age, explanation))
                return True
            return False

        def parse_osd_log_line(line):
            m = self.OSD_LOG_RE.match(line)
            if not m:
                return False
            osdno = int(m.group(1))
            tstamp = parse_timestamp(m.group(4), m.group(5))
            if not self.min_time or self.min_time < tstamp:
                hex = m.group(6)
                severity = m.group(7)
                rest = m.group(8)
                if parse_osd_log_scrub_line(rest, osdno, tstamp):
                    pass
                elif parse_osd_log_slow_line(rest, osdno, tstamp):
                    pass
                else:
                    raise ParseError("Unrecognized OSD log line: \"%s\"" % (line))
            return True

        def parse_timestamp(ymdhms, usec):
            return datetime.strptime(ymdhms, "%Y-%m-%d %H:%M:%S") \
                +timedelta(microseconds=int(usec))

        def parse_osd_set(s):
            # split()ting an empty string does not return the empty
            # list, but a list with a single empty string.  We have to
            # process this case separately.
            if s == '':
                return []
            return map(int, s.split(","))

        def osd_host(osd):
            return self.osd_to_host[osd]

        def parse_pg(line):

            m = self.PG_RE.match(line)
            if not m:
                return False
            pgid = m.group(1)
            bytes = int(m.group(2))
            status = m.group(5)
            up_set = parse_osd_set(m.group(6))
            up_primary = int(m.group(7))
            assert(up_set[0] == up_primary)
            acting_set = parse_osd_set(m.group(8))
            acting_primary = int(m.group(9))
            hosts = map(osd_host, acting_set)
            assert(acting_set[0] == acting_primary)
            self.pg[pgid] = PG(pgid, bytes=bytes, \
                               up=up_set, acting=acting_set,
                               hosts=hosts)
            return True

        def parse_osd_tree_host(line):
            m = self.OSD_TREE_HOST_RE.match(line)
            if not m:
                return False
            self.current_host = m.group(3)
            return True

        def parse_osd_tree_osd(line):
            m = self.OSD_TREE_OSD_RE.match(line)
            if not m:
                return False
            osdno = int(m.group(1))
            self.osd_to_host[osdno] = self.current_host
            return True

        def parse_osd_stats(line):
            m = self.OSD_STATS_RE.match(line)
            if not m:
                return False
            osdno = int(m.group(1))
            kb_used = int(m.group(2))
            kb_avail = int(m.group(3))
            kb = int(m.group(4))
            hb_in = parse_osd_set(m.group(5))
            hb_out = parse_osd_set(m.group(6))
            self.osd_to_kb_used[osdno] = kb_used
            return True

        self.scrub_count, self.shallow_count, self.deep_count = 0, 0, 0
        self.log = dict()
        self.osd_to_host = dict()
        self.osd_to_kb_used = dict()
        self.pg = dict()

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
        for time in sorted(self.log.keys()):
            event = self.log[time]
            pg = event.pg
            print(event)

    def add_scrub_start_event(self, end_event):

        def est_scrub_duration(size):
            scrub_init_duration=1
            usec = size/(self.scrub_rate_est/1e6)+scrub_init_duration
            return timedelta(microseconds=usec)
        if end_event.scrub_type == SCRUB_DEEP:
            pg_size = end_event.pg.bytes
            est_start = end_event.time-est_scrub_duration(pg_size)
            self.log[est_start] = ScrubEvent(est_start,
                                             scrub_type=end_event.scrub_type,
                                             start=True,
                                             pg=end_event.pg)

    def add_scrub_start_events(self):
        for time in self.log.keys():
            event = self.log[time]
            if isinstance(event, ScrubEvent):
                self.add_scrub_start_event(event)

ana = CephScrubLogAnalyzer(log=LOG,
                           min_time=datetime(2015,04,01))
ana.parse()