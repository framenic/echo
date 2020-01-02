#!/usr/bin/env python

"""
The MIT License (MIT)

Copyright (c) 2015 Maker Musings

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

# For a complete discussion, see http://www.makermusings.com

import email.utils
import json
import requests
import select
import socket
import struct
import sys
import time
import urllib
import uuid
import logging

# This XML is the minimum needed to define one of our virtual switches
# to the Amazon Echo

SETUP_XML = """<?xml version="1.0"?>
<root>
  <device>
    <deviceType>urn:MakerMusings:device:controllee:1</deviceType>
    <friendlyName>%(device_name)s</friendlyName>
    <manufacturer>Belkin International Inc.</manufacturer>
    <modelName>Emulated Socket</modelName>
    <modelNumber>3.1415</modelNumber>
    <UDN>uuid:Socket-1_0-%(device_serial)s</UDN>
  </device>
</root>
"""

HUE_SETUP_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
	<specVersion>
		<major>1</major>
		<minor>0</minor>
	</specVersion>
	<URLBase>%(host)%(port)/</URLBase>
	<device>
		<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
		<friendlyName>Philips hue (##URLBASE##)</friendlyName>
		<manufacturer>Royal Philips Electronics</manufacturer>
		<manufacturerURL>http://www.philips.com</manufacturerURL>
		<modelDescription>Philips hue Personal Wireless Lighting</modelDescription>
		<modelName>Philips hue bridge 2012</modelName>
		<modelNumber>929000226503</modelNumber>
		<modelURL>http://www.meethue.com</modelURL>
		<serialNumber>0017880ae670</serialNumber>
		<UDN>uuid:2f402f80-da50-11e1-9b23-0017880ae670</UDN>
		<serviceList>
			<service>
				<serviceType>(null)</serviceType>
				<serviceId>(null)</serviceId>
				<controlURL>(null)</controlURL>
				<eventSubURL>(null)</eventSubURL>
				<SCPDURL>(null)</SCPDURL>
			</service>
		</serviceList>
		<presentationURL>index.html</presentationURL>
	</device>
</root>"""

DEBUG = False

def dbg(msg):
    logging.debug(msg)


# A simple utility class to wait for incoming data to be
# ready on a socket.

class poller:
    def __init__(self):
        if 'poll' in dir(select):
            self.use_poll = True
            self.poller = select.poll()
        else:
            self.use_poll = False
        self.targets = {}

    def add(self, target, fileno = None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.register(fileno, select.POLLIN)
        self.targets[fileno] = target

    def remove(self, target, fileno = None):
        if not fileno:
            fileno = target.fileno()
        if self.use_poll:
            self.poller.unregister(fileno)
        del(self.targets[fileno])

    def poll(self, timeout = 0):
        if self.use_poll:
            ready = self.poller.poll(timeout)
        else:
            ready = []
            if len(self.targets) > 0:
                (rlist, wlist, xlist) = select.select(self.targets.keys(), [], [], timeout)
                ready = [(x, None) for x in rlist]
        for one_ready in ready:
            target = self.targets.get(one_ready[0], None)
            if target:
                target.do_read(one_ready[0])
 

# Base class for a generic UPnP device. This is far from complete
# but it supports either specified or automatic IP address and port
# selection.

class upnp_device(object):
    this_host_ip = None

    @staticmethod
    def local_ip_address():
        if not upnp_device.this_host_ip:
            temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                temp_socket.connect(('8.8.8.8', 53))
                upnp_device.this_host_ip = temp_socket.getsockname()[0]
            except:
                upnp_device.this_host_ip = '127.0.0.1'
            del(temp_socket)
            dbg("got local address of %s" % upnp_device.this_host_ip)
        return upnp_device.this_host_ip
        

    def __init__(self, listener, poller, port, root_url, server_version, persistent_uuid, protocol, other_headers = None, ip_address = None):
        self.listener = listener
        self.poller = poller
        self.port = port
        self.root_url = root_url
        self.server_version = server_version
        self.persistent_uuid = persistent_uuid
        self.protocol = protocol
        self.uuid = uuid.uuid4()
        self.other_headers = other_headers

        if ip_address:
            self.ip_address = ip_address
        else:
            self.ip_address = upnp_device.local_ip_address()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.ip_address, self.port))
        self.socket.listen(5)
        if self.port == 0:
            self.port = self.socket.getsockname()[1]
        self.poller.add(self)
        self.client_sockets = {}
        self.listener.add_device(self)
        self.lastsearch=0

    def fileno(self):
        return self.socket.fileno()

    def do_read(self, fileno):
        if fileno == self.socket.fileno():
            (client_socket, client_address) = self.socket.accept()
            self.poller.add(self, client_socket.fileno())
            self.client_sockets[client_socket.fileno()] = (client_socket, client_address)
        else:
            data, sender = self.client_sockets[fileno][0].recvfrom(4096)
            if not data:
                self.poller.remove(self, fileno)
                del(self.client_sockets[fileno])
            else:
                self.handle_request(data, sender, self.client_sockets[fileno][0], self.client_sockets[fileno][1])

    def handle_request(self, data, sender, socket, client_address):
        pass

    def get_name(self):
        return "unknown"
        
    def get_protocol(self):
        return self.protocol

    def respond_to_search(self, destination, search_target):
        #if (time.time()-self.lastsearch < 60):
        #    dbg("not responding to search for %s" % self.get_name())
        #else:
            self.lastsearch=time.time() 
            dbg("Responding to search for %s" % self.get_name())
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            location_url = self.root_url % {'ip_address' : self.ip_address, 'port' : self.port}
            message = ("HTTP/1.1 200 OK\r\n"
                      "CACHE-CONTROL: max-age=86400\r\n"
                      "DATE: %s\r\n"
                      "EXT:\r\n"
                      "LOCATION: %s\r\n"
                      "OPT: \"http://schemas.upnp.org/upnp/1/0/\"; ns=01\r\n"
                      "01-NLS: %s\r\n"
                      "SERVER: %s\r\n"
                      "ST: %s\r\n"
                      "USN: uuid:%s::%s\r\n" % (date_str, location_url, self.uuid, self.server_version, search_target, self.persistent_uuid, search_target))
            if self.other_headers:
                for header in self.other_headers:
                    message += "%s\r\n" % header
            message += "\r\n"
            temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp_socket.sendto(message, destination)


# This subclass does the bulk of the work to mimic a WeMo switch on the network.

class fauxhue(upnp_device):
    @staticmethod
    def make_uuid(name):
        return ''.join(["%x" % sum([ord(c) for c in name])] + ["%x" % ord(c) for c in "%sfauxhue!" % name])[:14]

    def __init__(self, name, listener, poller, ip_address, port, action_handler = None):
        self.lights = {}
        self.privates = {}
        self.ip_address = ip_address
        self.serial = self.make_uuid(name)
        self.name = name
        persistent_uuid = "Socket-1_0-" + self.serial
        other_headers = ['X-User-Agent: redsonic']
        upnp_device.__init__(self, listener, poller, port, "http://%(ip_address)s:%(port)s/description.xml", "Unspecified, UPnP/1.0, Unspecified", persistent_uuid, "hue", other_headers=other_headers, ip_address=ip_address)
        if action_handler:
            self.action_handler = action_handler
        else:
            self.action_handler = self
        dbg("FauxHue device '%s' ready on %s:%s" % (self.name, self.ip_address, self.port))

    def add_bulb (self, name, state=False, brightness=0, private=None):
        light = {
            "state": {
        "on": state,
        "bri": brightness,
        "hue": 0,
        "sat": 0,
        "xy": [0.0000, 0.0000],
        "ct": 0,
        "alert": "none",
        "effect": "none",
        "colormode": "hs",
        "reachable": True
        },
        "type": "Extended color light",
        "name": name,
        "modelid": "LCT001",
        "swversion": "65003148",
        "pointsymbol": {
        "1": "none",
        "2": "none",
        "3": "none",
        "4": "none",
        "5": "none",
        "6": "none",
        "7": "none",
                "8": "none"
        }
        }
        lightnum = len(self.lights) + 1
        self.lights[str(lightnum)] = light
        self.privates[str(lightnum)] = private
    def get_name(self):
        return self.name
    def send(self, socket, data):
        date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
        message = ("HTTP/1.1 200 OK\r\n"
                   "CONTENT-LENGTH: %d\r\n"
                   "CONTENT-TYPE: text/xml\r\n"
                   "DATE: %s\r\n"
                   "LAST-MODIFIED: Sat, 01 Jan 2000 00:01:15 GMT\r\n"
                   "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                   "X-User-Agent: redsonic\r\n"
                   "CONNECTION: close\r\n"
                   "\r\n"
                   "%s" % (len(data), date_str, data))
        #dbg(message)
        socket.send(message)

    def handle_request(self, data, sender, socket, client_address):
        tokens = data.split()
        if len(tokens) < 3 or tokens[2] != 'HTTP/1.1':
            dbg("Unknown request %s\n" % data)
            return
        requestdata = tokens[1].split('/')
        if tokens[0] == 'GET':
            if requestdata[1] == 'description.xml':
                dbg("Responding to description.xml for %s" % self.name)
                xml = HUE_SETUP_XML % {'host' : self.ip_address, 'port' : self.port}
                self.send(socket, xml)
            elif len(requestdata) == 4 and requestdata[3] == 'lights':
                data = json.dumps(self.lights)
                self.send(socket, data)
            elif len(requestdata) >= 5 and requestdata[3] == 'lights':
                data = json.dumps(self.lights[requestdata[4]])
                self.send(socket, data)
        elif tokens[0] == 'PUT':
            if len(requestdata) >= 5 and requestdata[3] == 'lights':
                light = requestdata[4]
                submission = data.split('\n')[6]
                command = json.loads(submission)
                responses = []
                for setting in command.keys():
                    value = command[setting]
                    private = self.privates[light]
                    self.lights[light]['state'][setting] = value
                    dbg ("Set %s to %s\n" % (setting, value))
                    if setting == "on":
                        if value == True:
                            self.action_handler.on(private,client_address[0])
                        elif value == False:
                            self.action_handler.off(private,client_address[0])
                    elif setting == "bri":
                        self.action_handler.dim(private,client_address[0],value)
                    apistring = "/lights/%s/state/%s" % (light, setting)
                    responses.append({"success":{apistring : command[setting]}})
                self.send(socket, json.dumps(responses))
        else:
            dbg("Unknown request: %s" % data)

    def on(self):
        return False

    def off(self):
        return True

class fauxmo(upnp_device):
    @staticmethod
    def make_uuid(name):
        return ''.join(["%x" % sum([ord(c) for c in name])] + ["%x" % ord(c) for c in "%sfauxmo!" % name])[:14]

    def __init__(self, name, listener, poller, ip_address, port, action_handler = None):
        self.serial = self.make_uuid(name)
        self.name = name
        self.ip_address = ip_address
        persistent_uuid = "Socket-1_0-" + self.serial
        other_headers = ['X-User-Agent: redsonic']
        upnp_device.__init__(self, listener, poller, port, "http://%(ip_address)s:%(port)s/setup.xml", "Unspecified, UPnP/1.0, Unspecified", persistent_uuid, "wemo", other_headers=other_headers, ip_address=ip_address)
        if action_handler:
            self.action_handler = action_handler
        else:
            self.action_handler = self
        dbg("FauxMo device '%s' ready on %s:%s" % (self.name, self.ip_address, self.port))

    def get_name(self):
        return self.name

    def handle_request(self, data, sender, socket, client_address):
        if data.find('GET /setup.xml HTTP/1.1') == 0:
            dbg("Responding to setup.xml for %s" % self.name)
            xml = SETUP_XML % {'device_name' : self.name, 'device_serial' : self.serial}
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml\r\n"
                       "DATE: %s\r\n"
                       "LAST-MODIFIED: Sat, 01 Jan 2000 00:01:15 GMT\r\n"
                       "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                       "X-User-Agent: redsonic\r\n"
                       "CONNECTION: close\r\n"
                       "\r\n"
                       "%s" % (len(xml), date_str, xml))
            socket.send(message)
        elif data.find('SOAPACTION: "urn:Belkin:service:basicevent:1#SetBinaryState"') != -1:
            success = False
            if data.find('<BinaryState>1</BinaryState>') != -1:
                # on
                dbg("Responding to ON for %s" % self.name)
                success = self.action_handler.on(client_address[0])
            elif data.find('<BinaryState>0</BinaryState>') != -1:
                # off
                dbg("Responding to OFF for %s" % self.name)
                success = self.action_handler.off(client_address[0])
            else:
                dbg("Unknown Binary State request:")
                dbg(data)
            if success:
                # The echo is happy with the 200 status code and doesn't
                # appear to care about the SOAP response body
                soap = ""
                date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
                message = ("HTTP/1.1 200 OK\r\n"
                           "CONTENT-LENGTH: %d\r\n"
                           "CONTENT-TYPE: text/xml charset=\"utf-8\"\r\n"
                           "DATE: %s\r\n"
                           "EXT:\r\n"
                           "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                           "X-User-Agent: redsonic\r\n"
                           "CONNECTION: close\r\n"
                           "\r\n"
                           "%s" % (len(soap), date_str, soap))
                socket.send(message)
        else:
            dbg(data)

    def on(self, private):
        return False

    def off(self, private):
        return True


# Since we have a single process managing several virtual UPnP devices,
# we only need a single listener for UPnP broadcasts. When a matching
# search is received, it causes each device instance to respond.
#
# Note that this is currently hard-coded to recognize only the search
# from the Amazon Echo for WeMo devices. In particular, it does not
# support the more common root device general search. The Echo
# doesn't search for root devices.

class upnp_broadcast_responder(object):
    TIMEOUT = 0

    def __init__(self):
        self.devices = []

    def init_socket(self):
        ok = True
        self.ip = '239.255.255.250'
        self.port = 1900
        try:
            #This is needed to join a multicast group
            self.mreq = struct.pack("4sl",socket.inet_aton(self.ip),socket.INADDR_ANY)

            #Set up server socket
            self.ssock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
            self.ssock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)

            try:
                self.ssock.bind(('',self.port))
            except Exception, e:
                dbg("WARNING: Failed to bind %s:%d: %s" , (self.ip,self.port,e))
                ok = False

            try:
                self.ssock.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,self.mreq)
            except Exception, e:
                dbg('WARNING: Failed to join multicast group:',e)
                ok = False

        except Exception, e:
            dbg("Failed to initialize UPnP sockets:",e)
            return False
        if ok:
            dbg("Listening for UPnP broadcasts")

    def fileno(self):
        return self.ssock.fileno()

    def do_read(self, fileno):
        data, sender = self.recvfrom(1024)
        if data:
	    if data.find('M-SEARCH') == 0:
		dbg(data)
            if data.find('M-SEARCH') == 0 and data.find('upnp:rootdevice') != -1:
                for device in self.devices:
                    if device.get_protocol() == "wemo":
                        time.sleep(0.1)
                        device.respond_to_search(sender, 'urn:Belkin:device:**')
            elif data.find('M-SEARCH') == 0 and data.find('urn:schemas-upnp-org:device:basic:1') != -1:
                for device in self.devices:
                    if device.get_protocol() == "hue":
                        time.sleep(0.1)
                        device.respond_to_search(sender, 'urn:schemas-upnp-org:device:basic:1')
            else:
                pass

    #Receive network data
    def recvfrom(self,size):
        if self.TIMEOUT:
            self.ssock.setblocking(0)
            ready = select.select([self.ssock], [], [], self.TIMEOUT)[0]
        else:
            self.ssock.setblocking(1)
            ready = True

        try:
            if ready:
                return self.ssock.recvfrom(size)
            else:
                return False, False
        except Exception, e:
            dbg(e)
            return False, False

    def add_device(self, device):
        self.devices.append(device)
        dbg("UPnP broadcast listener: new device registered")




