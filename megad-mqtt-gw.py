#!/usr/bin/env python
import aiohttp
import aiohttp.web
import argparse
import asyncio
from collections import defaultdict
import enum
import functools
import json
import logging
import logging.handlers
import paho.mqtt.client as mqtt
import re
import signal
import socket
import sys
import urllib.request


devices = None
logger = logging.getLogger(__name__)


class MegaDDevice(object):
    class ResponseMode(enum.Enum):
        EMPTY = 0
        DEVICE = 1

    response_mode_by_str = {
        'empty': ResponseMode.EMPTY,
        'blank': ResponseMode.EMPTY,
        'device': ResponseMode.DEVICE
    }

    def _parse_port_html(self, response_body):
        def extract_attrs(s):
            attrs = {}
            for a in s.split(' '):
                a_idx = a.find('=')
                if a_idx >= 0:
                    val = a[a_idx + 1:]
                    if len(val) > 0 and val[0] in ['"', "'"]:
                        val = val[1:-1]
                    attrs[a[:a_idx]] = val
            return attrs

        props = {}
        for it in re.finditer(r'<input ([^>]+)>', response_body):
            it_attrs = extract_attrs(it.group(1))
            if 'name' in it_attrs and 'value' in it_attrs:
                props[it_attrs['name']] = it_attrs['value']
        for it in re.finditer(r'<select\s+name=([^>]+)>(.*?)</select>', response_body):
            m = re.search(r'<option([^>]*)selected([^>]*)>', it.group(2))
            if m:
                opt_attrs = extract_attrs(m.group(1))
                opt_attrs.update(extract_attrs(m.group(2)))
                if 'value' in opt_attrs:
                    props[it.group(1)] = opt_attrs['value']
        return props

    def _query_device(self, device_address, device_password):
        # query MegaID
        megaid_html = urllib.request.urlopen('http://' + device_address + '/' + device_password + '/?cf=2', timeout=10).\
            read().decode()
        m = re.search(r'<input[^>]+name=mdid\s[^>]+value="([^"]*)">', megaid_html)
        megaid = m.group(1) if m and len(m.group(1)) > 0 else device_address

        # read megad configuration (for later checking)
        megacf_html_req = urllib.request.urlopen('http://' + device_address + '/' + device_password + '/?cf=1', timeout=10)
        megacf = {'_local_ip': megacf_html_req.fp.raw._sock.getsockname()[0]}
        megacf_html = megacf_html_req.read().decode()
        for it in re.finditer(r'<input[^>]+name=([^> ]+)\s[^>]*value=([^> ]+)>', megacf_html):
            megacf[it.group(1)] = it.group(2).strip('"')

        # read ports confiruration
        megaver_html = urllib.request.urlopen('http://' + device_address + '/' + device_password + '/', timeout=10).\
            read().decode()
        megaver = 328
        if 'MegaD-2561' in megaver_html:
            megaver = 2561

        ports = {}
        if megaver == 328:
            ports_html = urllib.request.urlopen('http://' + device_address + '/' + device_password).read().decode()
            for it in re.finditer(r'<a href=([^<>]*?\?pt=.*?)>(.*?)</a>', ports_html):
                port_html = urllib.request.urlopen('http://' + device_address + it.group(1)).read().decode()
                port_props = self._parse_port_html(port_html)
                port_props['name'] = it.group(2)
                if 'pn' in port_props:
                    ports['p' + port_props['pn']] = port_props
                else:
                    logger.warning('incorrect or unsupported port description received from address http://' +
                          device_address + it.group(1))
        elif megaver == 2561:
            for port_list_url in ['/', '/?cf=3', '/?cf=4']:
                ports_html = urllib.request.urlopen('http://' + device_address + '/' + device_password + port_list_url).\
                    read().decode()
                for it in re.finditer(r'<a href=([^<>]*?\?pt=.*?)>(.*?)</a>', ports_html):
                    port_html = urllib.request.urlopen('http://' + device_address + it.group(1)).read().decode()
                    port_props = self._parse_port_html(port_html)
                    port_props['name'] = it.group(2)
                    if 'pn' in port_props:
                        ports['p' + port_props['pn']] = port_props
                    else:
                        logger.warning('incorrect or unsupported port description received from address http://' +
                              device_address + it.group(1))

        return megaid, megacf, ports

    def __init__(self, cfg):
        self.address = cfg['address']
        self.password = cfg['password']
        self.response_mode = MegaDDevice.response_mode_by_str[cfg.get('response_mode', 'device')]
        self.device_base_url = 'http://' + self.address + '/' + self.password + '/'
        self.mega_id, self.mega_cf, self.ports = self._query_device(self.address, self.password)
        self.mega_cf_checked = False
        self.device_id = 'megad_' + self.mega_id
        self.device_name = 'MegaD ' + self.mega_id + ' (' + self.address + ')'

    def get_ports(self):
        return self.ports

    def check_config(self):
        # <input name=sip value=192.168.1.110:19780>
        # <input name=sct maxlength=15 value="megad">
        result = []
        if self.mega_cf_checked:
            return result
        if 'sip' in self.mega_cf and '_local_ip' in self.mega_cf and \
                        self.mega_cf['sip'].split(':')[0] != self.mega_cf['_local_ip']:
            result.append('MegaD {} server IP is incorrect (configured {}, but must be {})'.format(
                self.mega_id, self.mega_cf['sip'].split(':')[0], self.mega_cf['_local_ip']))
        if 'sct' in self.mega_cf and self.mega_cf['sct'] != 'megad':
            result.append('MegaD {} script is incorrect (configured {}, but must be "megad")'.format(
                self.mega_id, self.mega_cf['sct']))
        self.mega_cf_checked = True
        return result

    async def fetch_ports_state(self):
        #state = urllib.request.urlopen(self.device_base_url + '?cmd=all').read().decode()
        async with aiohttp.ClientSession() as session:
            async with session.get(self.device_base_url + '?cmd=all') as resp:
                if resp.status != 200:
                    return []
                state = await resp.text()

        updated = []
        for idx, val in enumerate(state.split(';')):
            p_name = 'p' + str(idx)
            if p_name in self.ports:
                if val.startswith('OFF'):
                    if 'value' not in self.ports[p_name] or self.ports[p_name]['value'] != 0:
                        self.ports[p_name]['value'] = 0
                        updated.append(p_name)
                elif val.startswith('ON'):
                    if 'value' not in self.ports[p_name] or self.ports[p_name]['value'] != 1:
                        self.ports[p_name]['value'] = 1
                        updated.append(p_name)
                elif val.isnumeric():
                    if 'value' not in self.ports[p_name] or self.ports[p_name]['value'] != int(val):
                        self.ports[p_name]['value'] = int(val)
                        updated.append(p_name)
                else:
                    logger.warning('Can\'t state of port. MegaD ID: ' + self.mega_id + '. Port: ' + str(idx) +
                                   '. Value: ' + val)
        return updated

    async def send_message(self, control, command):
        logger.debug('HTTP message: ctrl=%s cmd=%s', control, command)

        #ports_html = urllib.request.urlopen(self.device_base_url + '?cmd=' + control[1:] + ':' + command).read().decode()
        async with aiohttp.ClientSession() as session:
            async with session.get(self.device_base_url + '?cmd=' + control[1:] + ':' + command) as resp:
                if resp.status != 200:
                    return None
                ports_html = await resp.text()
                if ports_html == 'Done':
                    return command
        return None


class MegaDDevicesSet(object):
    class MQTTPortDesc:
        def __init__(self, mutable=[], constant=[]):
            self.mutable = mutable
            self.constant = constant
            self.subscribe = []

    def _make_port_topics(self, topic_prefix, parameters, keywords):
        result_mutable = []
        result_const = []
        for k, v in parameters.items():
            if type(v) is dict:
                r_m, r_c = self._make_port_topics(topic_prefix + '/' + k, v, keywords)
                result_mutable.extend(r_m)
                result_const.extend(r_c)
            else:
                if type(v) is str and '{value}' in v:
                    result_mutable.append((topic_prefix + '/' + k, v))
                else:
                    result_const.append((topic_prefix + '/' + k, v))
        return result_mutable, result_const

    def __init__(self, cfg):
        cf_devices = cfg['devices']
        self.devices = {}
        for cf_dev in cf_devices:
            try:
                dev = MegaDDevice(cf_dev)
                self.devices[dev.device_id] = dev
                logger.info('Added device ' + dev.device_id)
            except Exception as e:
                logger.warning('Error at add device ' + cf_dev.get('id', '<ID UNSPECIFIED>') + ' (' +
                                cf_dev.get('address', '<ADDRESS UNSPECIFIED>') + '). Exception details: ' + str(e))
                logger.warning('Device {} is excluded from processing.'.format(cf_dev.get('address', '<ADDRESS UNSPECIFIED>')))

        cf_mqtt = cfg['mqtt']
        self.notify_topic = cf_mqtt.get('notify_topic', None)
        self.devices_mqtt_topic = defaultdict(lambda: defaultdict(lambda: MegaDDevicesSet.MQTTPortDesc()))
        self.devices_mqtt_name_topic = defaultdict(lambda: [])
        for dev_id, dev in self.devices.items():
            self.devices_mqtt_name_topic[dev.device_id] = cf_mqtt['device_name_topic'].format(device_id=dev.device_id)
            for port, desc in dev.get_ports().items():
                port_prefix = cf_mqtt['device_port_topic'].format(device_id=dev.device_id, port=port)
                if 'pty' in desc and 'm' in desc and desc["pty"] + '/' + desc["m"] in cf_mqtt["template"]:
                    template = cf_mqtt["template"][desc["pty"] + '/' + desc['m']]
                elif 'pty' in desc and desc["pty"] in cf_mqtt["template"]:
                    template = cf_mqtt["template"][desc["pty"]]
                else:
                    template = None
                if template is not None:
                    r = self._make_port_topics(port_prefix, template, {'device_id': dev.device_id, **desc})
                    self.devices_mqtt_topic[dev.device_id][port].mutable = r[0]
                    self.devices_mqtt_topic[dev.device_id][port].constant = r[1]
                    self.devices_mqtt_topic[dev.device_id][port].subscribe = [port_prefix + '/on']

        self.http_client = None
        self.mqtt_client = None

    def set_servers(self, http_server, mqtt_client):
        self.http_server = http_server
        self.mqtt_client = mqtt_client

    async def on_http_pool(self):
        if self.http_server is None or self.mqtt_client is None:
            return
        try:
            for megad_id, dev in self.devices.items():
                updated_ports = await dev.fetch_ports_state()
                for port in updated_ports:
                    v_keyword = {'device_id': dev.device_id, **dev.get_ports()[port]}
                    for t, v in self.devices_mqtt_topic[dev.device_id][port].mutable:
                        logger.debug('HTTP pool: Sending MQTT message to topic {}'.format(t))
                        await self.mqtt_client.async_publish(t, v.format(**v_keyword), 0, True)
        except Exception as e:
            logger.error('Exception on HTTP MegaD message processing. Exception detail: ' + str(e))

    async def on_http_message_postprocess(self, address):
        if self.http_server is None or self.mqtt_client is None:
            return
        megad_id = 'megad_' + str(address)
        try:
            dev = self.devices.get(megad_id, None)
            if dev is None:
                return
            updated_ports = await dev.fetch_ports_state()
            for port in updated_ports:
                v_keyword = {'device_id': dev.device_id, **dev.get_ports()[port]}
                for t, v in self.devices_mqtt_topic[dev.device_id][port].mutable:
                    logger.debug('HTTP postprocess: ending MQTT message to topic {}'.format(t))
                    await self.mqtt_client.async_publish(t, v.format(**v_keyword), 0, True)
        except Exception as e:
            logger.error('Exception on HTTP MegaD message processing. Exception detail: ' + str(e))

    async def on_http_message(self, address, parameters):
        if self.http_server is None or self.mqtt_client is None:
            return
        logger.debug('HTTP message from {} with parameters {}'.format(address, parameters))

        if 'pt' not in parameters:
            return ''

        megad_id = 'megad_' + str(address)
        port = 'p' + parameters.get('pt', None)
        try:
            dev = self.devices.get(megad_id, None)
            if dev is None:
                return ''
            port_data = dev.get_ports().get(port, None)
            if port_data is None:
                return ''
            if port_data['pty'] == '0':
                port_data['value'] = parameters.get('m', 1)
                v_keyword = {'device_id': dev.device_id, **port_data}
                for t, v in self.devices_mqtt_topic[dev.device_id][port].mutable:
                    logger.debug('HTTP message: Sending MQTT message to topic {}'.format(t))
                    await self.mqtt_client.async_publish(t, v.format(**v_keyword) if type(v) is str else str(v), 0, True)
                result = ''
                if dev.response_mode == MegaDDevice.ResponseMode.DEVICE:
                    result = port_data.get('ecmd', '')
                logger.debug('HTTP response is {}'.format(result))
                return result
            else:
                pass   # TODO:
        except Exception as e:
            logger.error('Exception on HTTP MegaD message processing. Exception detail: ' + str(e))
        return ''

    async def on_mqtt_connect(self):
        if self.http_server is None or self.mqtt_client is None:
            return
        self.mqtt_value_topic = {}
        for dev_id, dev in self.devices.items():
            await dev.fetch_ports_state()
            await self.mqtt_client.async_publish(self.devices_mqtt_name_topic[dev.device_id], dev.device_name, 0, True)
            for port, port_data in dev.get_ports().items():
                v_keyword = {'device_id': dev.device_id, **port_data}
                for t, v in self.devices_mqtt_topic[dev.device_id][port].constant:
                    await self.mqtt_client.async_publish(t, v.format(**v_keyword) if type(v) is str else str(v), 0, True)
                for t, v in self.devices_mqtt_topic[dev.device_id][port].mutable:
                    await self.mqtt_client.async_publish(t, v.format(**v_keyword) if type(v) is str else str(v), 0, True)
                for t in self.devices_mqtt_topic[dev.device_id][port].subscribe:
                    await self.mqtt_client.async_subscribe(t)
            for err_msg in dev.check_config():
                logger.warning('Device check: {}'.format(err_msg))
                if self.notify_topic is not None:
                    await self.mqtt_client.async_publish(self.notify_topic, err_msg, 0, False)

    async def on_mqtt_message(self, topic, payload):
        if self.http_server is None or self.mqtt_client is None:
            return
        try:
            parts = topic.split('/')
            if mqtt.topic_matches_sub('/devices/+/controls/+/on', topic):
                device_id = parts[2]
                control = parts[4]
                dev = self.devices.get(device_id, None)
                if dev is None:
                    return

                logger.debug('MQTT inbound message for {} with parameters {}. Result is {}'.format(device_id, control, payload.decode('utf-8')))
                await dev.send_message(control, payload.decode('utf-8'))

                updated_ports = await dev.fetch_ports_state()
                for port in updated_ports:
                    v_keyword = {'device_id': dev.device_id, **dev.get_ports()[port]}
                    for t, v in self.devices_mqtt_topic[device_id][port].mutable:
                        logger.debug('MQTT outbound message for topic {}'.format(t))
                        await self.mqtt_client.async_publish(t, v.format(**v_keyword), 0, True)
        except Exception as e:
            logger.error('Exception on MQTT message processing. Exception detail: ' + str(e))


###############################################################################
#                    M Q T T     Protocol handler                             #
###############################################################################


PROTOCOL_31 = '3.1'
PROTOCOL_311 = '3.1.1'

DEFAULT_PORT = 1883
DEFAULT_KEEPALIVE = 60
DEFAULT_QOS = 0
DEFAULT_RETAIN = False
DEFAULT_PROTOCOL = PROTOCOL_311

MAX_RECONNECT_WAIT = 300  # seconds


class MQTT(object):
    def __init__(self, loop, broker, port, client_id, keepalive, username, password,
                 certificate, client_key, client_cert, tls_insecure, protocol,
                 async_on_connect, async_on_message):
        self.loop = loop
        self.broker = broker
        self.port = port
        self.keepalive = keepalive
        self.async_on_connect_cb = async_on_connect
        self.async_on_message_cb = async_on_message
        self._paho_lock = asyncio.Lock(loop=loop)
        self._mqttc = mqtt.Client(client_id='' if client_id is None else client_id,
                                  protocol=mqtt.MQTTv31 if protocol == PROTOCOL_31 else mqtt.MQTTv311)
        if username is not None:
            self._mqttc.username_pw_set(username, password)
        if certificate is not None:
            self._mqttc.tls_set(certificate, certfile=client_cert, keyfile=client_key)
        if tls_insecure is not None:
            self._mqttc.tls_insecure_set(tls_insecure)
        self._mqttc.on_connect = self._mqtt_on_connect
        self._mqttc.on_disconnect = self._mqtt_on_disconnect
        self._mqttc.on_message = self._mqtt_on_message

    async def async_publish(self, topic, payload, qos=DEFAULT_QOS, retain=DEFAULT_RETAIN):
        with (await self._paho_lock):
            await self.loop.run_in_executor(None, self._mqttc.publish, topic, payload, qos, retain)

    async def async_on_message(self, topic, payload, qos):
        await self.async_on_message_cb(topic, payload)

    async def async_connect(self):
        tries = 0
        while True:
            try:
                result = await self.loop.run_in_executor(None, self._mqttc.connect, self.broker, self.port, self.keepalive)
                if result == 0:
                    return
            except socket.error:
                pass
            logger.warning("Disconnected from MQTT. Trying to reconnect.")
            await asyncio.sleep(min(2**tries, MAX_RECONNECT_WAIT))
            tries += 1

    async def async_on_connect(self):
        await self.async_on_connect_cb()

    def async_start(self):
        return self.loop.run_in_executor(None, self._mqttc.loop_start)

    def async_stop(self):
        def stop():
            self._mqttc.disconnect()
            self._mqttc.loop_stop()

        return self.loop.run_in_executor(None, stop)

    async def async_subscribe(self, topic, qos=DEFAULT_QOS):
        with (await self._paho_lock):
            result, mid = await self.loop.run_in_executor(None, self._mqttc.subscribe, topic, qos)
            await asyncio.sleep(0, loop=self.loop)
        _raise_on_error(result)

    async def async_unsubscribe(self, topic):
        with (await self._paho_lock):
            result, mid = await self.loop.run_in_executor(None, self._mqttc.unsubscribe, topic)
            await asyncio.sleep(0, loop=self.loop)
        _raise_on_error(result)

    def _async_add_job(self, target, *args):
        if asyncio.iscoroutine(target):
            self.loop.create_task(target)
        elif asyncio.iscoroutinefunction(target):
            self.loop.create_task(target(*args))
        else:
            self.loop.run_in_executor(None, target, *args)

    def _mqtt_on_connect(self, _mqttc, _userdata, _flags, result_code):
        if result_code != mqtt.CONNACK_ACCEPTED:
            logger.error('Unable to connect to the MQTT broker: %s', mqtt.connack_string(result_code))
            self._mqttc.disconnect()
            return
        logger.debug("Connected to MQTT.")
        self.loop.call_soon_threadsafe(self._async_add_job, self.async_on_connect)

    def _mqtt_on_disconnect(self, _mqttc, _userdata, result_code):
        logger.debug("Disconnected from MQTT. Result code: {} ({}) ".format(mqtt.error_string(result_code), result_code))

    def _mqtt_on_message(self, _mqttc, _userdata, msg):
        self.loop.call_soon_threadsafe(self._async_add_job, self.async_on_message, msg.topic, msg.payload, msg.qos)


def _raise_on_error(result):
    if result != 0:
        raise Exception('Error talking to MQTT: {}'.format(mqtt.error_string(result)))


###############################################################################
#                    H T T P     Protocol handler                             #
###############################################################################


async def handler_http_postprocess(host):
    await devices.on_http_message_postprocess(host)


async def handler_http(request):
    if request.rel_url.path != '/megad':
        return aiohttp.web.Response(text="ERROR: Incorrect path")
    peername = request.transport.get_extra_info('peername')
    if peername is None:
        return aiohttp.web.Response(text="ERROR: Internal error - unknown remote address of peer")
    host, port = peername
    command = await devices.on_http_message(host, request.rel_url.query)

    request.transport._loop.create_task(handler_http_postprocess(host))

    # hack to send headers and body in one packet as required by MegaD-328
    request._writer.set_tcp_cork(True)
    request._writer.set_tcp_nodelay(False)
    response = aiohttp.web.Response(text=command)
    response.force_close()
    return response


###############################################################################
#                                 M A I N                                     #
###############################################################################


class MyLogger(object):
    def __init__(self, logger, level):
        self.logger = logger
        self.level = level

    def write(self, message):
        if message.rstrip() != "":
            self.logger.log(self.level, message.rstrip())

    def flush(self):
        pass


def main_configure():
    global devices
    global logger

    parser = argparse.ArgumentParser(description='WirenBoard driver for MegaDevices (ab-log.ru).')
    parser.add_argument('--config', default='/etc/megad-mqtt-gw.conf', help='name of configuration file')
    parser.add_argument('--log', default='/var/log/megad-mqtt-gw.log', help='name of log file')
    parser.add_argument('--debug', action='store_true', default=False, help='output more information to log and console')
    args = parser.parse_args()

    try:
        cfg = json.load(open(args.config, 'rt', encoding='utf-8'))
    except Exception as e:
        print('Can\'t load configuration from path "' + args.config + '". Exception detail: ' + str(e))
        return None

    try:
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG if args.debug else logging.INFO)
        handler = logging.handlers.TimedRotatingFileHandler(args.log, when='midnight', backupCount=3)
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        if args.debug:
            handler_console = logging.StreamHandler(sys.stdout)
            handler_console.setFormatter(formatter)
            logger.addHandler(handler_console)
        sys.stdout = MyLogger(logger, logging.INFO)
        sys.stderr = MyLogger(logger, logging.ERROR)

        logger.info('------------- MegaD-MQTT gateway started -------------------')
    except Exception as e:
        print('Can\'t initialize logging with path "' + args.log + '". Exception details: ' + str(e))
        return None

    try:
        logger.info('Initializing')
        devices = MegaDDevicesSet(cfg)
    except Exception as e:
        logger.error('Error at initializing. Exception detail: ' + str(e))
        return None

    return cfg


def async_pooling_cb(loop, interval):
    async def pool():
        global devices
        await devices.on_http_pool()
        if loop.is_running():
            loop.call_later(interval, async_pooling_cb, loop, interval)

    loop.create_task(pool())


async def main_setup(loop, conf_http, conf_mqtt):
    server_http = aiohttp.web.Server(handler_http, loop=loop)

    server_mqtt = MQTT(loop,
                       broker=conf_mqtt.get('address', '127.0.0.1'), port=conf_mqtt.get('port', DEFAULT_PORT),
                       client_id=conf_mqtt.get('client_id', 'megad-mqtt-gw'), keepalive=DEFAULT_KEEPALIVE,
                       username=conf_mqtt.get('username', None), password=conf_mqtt.get('password', None),
                       certificate=conf_mqtt.get('certificate', None), client_key=conf_mqtt.get('client_key', None),
                       client_cert=conf_mqtt.get('client_cert', None), tls_insecure=conf_mqtt.get('tls_insecure', None),
                       protocol=conf_mqtt.get('protocol', DEFAULT_PROTOCOL),
                       async_on_message=devices.on_mqtt_message, async_on_connect=devices.on_mqtt_connect)

    devices.set_servers(server_http, server_mqtt)

    await loop.create_server(server_http,
                             conf_http.get('address', '0.0.0.0'),
                             conf_http.get('port', '19780'))

    await server_mqtt.async_connect()
    server_mqtt.async_start()

    # Create timer for MegaD devices periodic pooling
    pool_interval = conf_http.get('pool', 0)
    if pool_interval > 0:
        loop.call_later(pool_interval, async_pooling_cb, loop, pool_interval)

    return server_http, server_mqtt


def async_exit(server_mqtt):
    global asyncio_loop

    try:
        asyncio_loop.run_until_complete(server_mqtt.async_stop())
    except Exception:
        pass
    asyncio_loop.stop()


def main():
    global asyncio_loop

    conf = main_configure()
    if conf is None:
        print('Error at loading configuration. For detail see messages above')
        return

    asyncio_loop = asyncio.get_event_loop()

    try:
        task = asyncio_loop.create_task(main_setup(asyncio_loop, conf['http'], conf['mqtt']))
        asyncio_loop.run_until_complete(task)
        server_http, server_mqtt = task.result()
    except Exception as e:
        asyncio_loop.close()
        print('Error at startup. Exception detail: ' + str(e))

    for signame in ('SIGINT', 'SIGTERM'):
        asyncio_loop.add_signal_handler(getattr(signal, signame), functools.partial(async_exit, server_mqtt))

    asyncio_loop.run_forever()


if __name__ == '__main__':
    main()
