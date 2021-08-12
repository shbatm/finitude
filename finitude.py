"""
finitude.py

Exports runtime data from a Carrier Infinity or Bryant Evolution HVAC system
to Prometheus.

Prometheus can query us very often (every second if desired) because we are
constantly listening to the HVAC's RS-485 bus and updating our internal state.
"""

import logging, prometheus_client, threading, time, yaml

import frames


class RequestError(Exception):
    pass


LOGGER = logging.getLogger('finitude')


class HvacMonitor:
    FRAME_COUNT = prometheus_client.Counter('finitude_frames',
                                            'number of frames received', ['name'])
    IS_SYNC = prometheus_client.Gauge('finitude_synchronized',
                                      '1 if reader is synchronized to bus', ['name'])
    DESYNC_COUNT = prometheus_client.Counter('finitude_desyncs',
                                             'number of desynchronizations', ['name'])
    RECONNECT_COUNT = prometheus_client.Counter('finitude_reconnects',
                                                'number of stream reconnects', ['name'])
    STORED_FRAMES = prometheus_client.Gauge('finitude_stored_frames',
                                            'number of frames stored', ['name'])
    FRAME_SEQUENCE_LENGTH = prometheus_client.Gauge('finitude_frame_sequence_length',
                                                    'length of sequence', ['name'])
    DEVINFO = prometheus_client.Info('finitude_device',
                                     'info table from each device on the bus',
                                     ['name', 'device'])
    TABLE_NAME_MAP = {
        'AirHandler06': 'airhandler',
        'AirHandler16': 'airhandler',
        'TStatCurrentParams': '',
        'TStatZoneParams': '',
        'TStatVacationParams': 'vacation',
        'HeatPump01': 'heatpump',
        'HeatPump02': 'heatpump',
        }
    GAUGES = {}
    CV = threading.Condition()

    def __init__(self, name, path):
        self.name, self.path = name, path
        self.stream, self.bus = None, None
        self.synchronized = False
        self.register_to_rest = {}
        self.frame_to_index = {}
        self.frames = []  # squashed
        HvacMonitor.IS_SYNC.labels(name=self.name).set_function(lambda s=self: s.synchronized)
        HvacMonitor.STORED_FRAMES.labels(name=self.name).set_function(lambda s=self: len(s.frame_to_index))
        HvacMonitor.FRAME_SEQUENCE_LENGTH.labels(name=self.name).set_function(lambda s=self: len(s.frames))

    def open(self):
        LOGGER.info(f'connecting to {self.name} at {self.path}')
        self.stream = frames.StreamFactory(self.path)
        self.bus = frames.Bus(self.stream, report_crc_error=self._report_crc_error)

        HvacMonitor.RECONNECT_COUNT.labels(name=self.name).inc()

    def process_frame(self, frame):
        self.synchronized = True
        if frame.func == frames.Function.ACK06 and frame.length >= 3:
            (name, values, rest) = frame.parse_register()
            (basename, paren, num) = name.partition('(')
            if values:
                if basename == 'DeviceInfo':
                    self.DEVINFO.labels(name=self.name, device=frames.ParsedFrame.get_printable_address(frame.source)).info(values)
                else:
                    tablename = self.TABLE_NAME_MAP.get(basename, basename)
                    for (k, v) in values.items():
                        self._set_gauge(tablename, k, v)                    
            self._storeframe(frame, name, rest)

    def _storeframe(self, frame, name, rest):
        """If rest is a statechange on name, store the frame."""
        lastrest = self.register_to_rest.get(name) if rest else None
        if lastrest == (rest or None):
            return
        self.register_to_rest[name] = rest
        index = self.frame_to_index.get(frame.data)
        if index is None:
            index = len(self.frame_to_index) + 1
            self.frame_to_index[frame.data] = index
        self.frames.append((time.time(), name, index))

    def _set_gauge(self, tablename, itemname, v):
        if isinstance(v, str):
            # TODO: emit as a label?
            return
        desc = ''
        divisor = 1
        (pre, times, post) = itemname.partition('Times7')
        if times and not post:
            itemname = pre
            divisor = 7
        (pre, times, post) = itemname.partition('Times16')
        if times and not post:
            itemname = pre
            divisor = 16
        for words in ['RPM', 'CFM']:
            (pre, word, post) = itemname.partition(words)
            if word:
                itemname = f'{pre}{"_" if pre else ""}{word.lower()}{"_" if post else ""}{post}'
                break
        if tablename:
            gaugename = f'finitude_{tablename}_{itemname.lower()}'
        else:
            gaugename = f'finitude_{itemname}'
        with HvacMonitor.CV:
            gauge = HvacMonitor.GAUGES.get(gaugename)
            if gauge is None:
                gauge = prometheus_client.Gauge(gaugename, desc, ['name'])
                HvacMonitor.GAUGES[gaugename] = gauge
            gauge.labels(name=self.name).set(v / divisor)

    def _report_crc_error(self):
        if self.synchronized:
            self.synchronized = False
            HvacMonitor.DESYNC_COUNT.labels(name=self.name).inc()

    def run(self):
        self.open()  # at startup, fail if we can't open
        while True:
            try:
                if self.stream is None:
                    self.open()
                frame = frames.ParsedFrame(self.bus.read())
                HvacMonitor.FRAME_COUNT.labels(name=self.name).inc()
                self.process_frame(frame)
            except OSError:
                LOGGER.exception('exception in frame processor, reconnecting')
                time.sleep(1)
                self.stream, self.bus = None, None


class Finitude:
    def __init__(self, config):
        self.config = config
        port = self.config.get('port')
        if not port:
            self.config['port'] = 8000

    def start_http_server(self, port=0):
        if not port:
            port = self.config['port']
        LOGGER.info(f'serving on port {port}')
        prometheus_client.start_http_server(port)

    def start_listeners(self, listeners={}):
        if not listeners:
            listeners = self.config['listeners']
        for (name, path) in listeners.items():
            monitor = HvacMonitor(name, path)
            threading.Thread(target=monitor.run, name=name).start()


def main(args):
    logging.basicConfig(level=logging.INFO)
    configfile = 'finitude.yml'
    if len(args) > 1:
        configfile = args[1]

    config = yaml.safe_load(open(configfile, 'rt')) or {}
    if config:
        LOGGER.info(f'using configuration file {configfile}')
    else:
        LOGGER.info(f'configuration file {configfile} was empty; ignored')
    f = Finitude(config)
    f.start_http_server()
    f.start_listeners()


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))
