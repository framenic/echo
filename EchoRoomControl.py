""" EchoRoomControl.py 
    This is the room control script used to controll the lighting and other systems 
    at my appartment. The devices controlled are a Fargo R8 relay unit and192.1 several 
    pingpong light systems (  ) running on ESP8266's 

"""

import fauxmo
import logging
import time
import urllib2

from debounce_handler import debounce_handler

logging.basicConfig(level=logging.DEBUG)


#urllib2.urlopen()

fargourl="http://192.168.2.14/api/relay/"

triggers={"fargo":[
            {"trig":{"colored lights": 52000},"relay":0},
            {"trig":{"white lights": 52010},"relay":1},
            {"trig":{"wizard light": 52020},"relay":2},
            {"trig":{"tentacle lamp": 52030},"relay":3},
            {"trig":{"desk lamp": 52040},"relay":4},
            {"trig":{"air filter": 52050},"relay":5}
        ],"generic":[
            {"trig":{"pong one":52100},"on":"http://192.168.2.138/load.cmd?22","off":"http://192.168.2.138/load.cmd?0"},
            {"trig":{"pong two":52100},"on":"http://192.168.2.138/setScene.cmd?scene=1","off":"http://192.168.2.138/setScene.cmd?scene=1"}
        ]
    }

class generic_handler(debounce_handler):
    def initialize(self,triggers,on,off,responder,poller)
        self.onurl=on
        self.offurl=off
        for trig, port in triggers.items():
            fauxmo.fauxmo(trig, responder, poller, None, port, self)


    def act(self,client_address,state):
        print "Generic Handler ",state,"from client@", client_address
        if (state):
            code=urllib2.urlopen(self.onurl).getcode()
        else:
            code=urllib2.urlopen(self.offurl).getcode()
        if (code==200):
            return True
        else:
            return False


class fargo_handler(debounce_handler):   
    def initialize(self,triggers,relaynumber,responder,poller):
        self.relaynumber=relaynumber
        for trig, port in triggers.items():
            fauxmo.fauxmo(trig, responder, poller, None, port, self)

    def act(self,client_address,state):
        print "Fargo Handler "+str(self.relaynumber),state,"from client@", client_address
        if (state):
            code=urllib2.urlopen(fargourl+str(self.relaynumber+1)+"/on").getcode()
        else:
            code=urllib2.urlopen(fargourl+str(self.relaynumber+1)+"/off").getcode()
        if (code==200):
            return True
        else:
            return False

if __name__ == "__main__":
    # Startup the fauxmo server
    fauxmo.DEBUG = True
    p = fauxmo.poller()
    u = fauxmo.upnp_broadcast_responder()
    u.init_socket()
    p.add(u)

    # Register the device callback as a fauxmo handler
    for triglist in triggers["fargo"]:
        fargo_handler().initialize(triglist["trig"],triglist["relay"],u,p)
    for triglist in triggers["generic"]:
        generic_handler().initialize(triglist["trig"],triglist["on"],triglist["off"],u,p)

    # Loop and poll for incoming Echo requests
    logging.debug("Entering fauxmo polling loop")
    while True:
        try:
            # Allow time for a ctrl-c to stop the process
            p.poll(100)
            time.sleep(0.1)
        except Exception, e:
            logging.critical("Critical exception: " + str(e))
            break