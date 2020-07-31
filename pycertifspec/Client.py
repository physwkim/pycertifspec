import numpy as np
import socket
from functools import reduce
import time as tm
import threading
import struct
import collections
from .EventTypes import EventTypes
from .DataTypes import DataTypes
from .Motor import Motor
from .Var import Var
from .ArrayVar import ArrayVar
from .SpecSocket import SpecSocket, SpecMessage
from .SpecError import SpecError
from typing import Callable, List

class Client:
    def __init__(self, host="localhost", port=None, port_range=(6510, 6530), ports=[], timeout=0.1):
        """
        Attempt to create a connection to SPEC

        Attributes:
            host (string): The address of the SPEC server
            port (int): If exact port is known, the port to connect to
            port_range (tuple): Range of ports to scan (end is inclusive)
            ports (list): List of ports to scan
            timeout (float): Time to wait for answer before trying the next port
        """
        self.sock = SpecSocket()
        self.sock.connect_spec(host, port, port_range, ports, timeout)

        threading.Thread(target=self._listener_thread).start()

        self.subscribe("error", None, nowait=True)

        self._sn_counter = 0
        self._sn_callbacks = {}
        self._reply_events = {}
        self._reply_msgs = {}
        self._send_lock = threading.Lock()

        self._subscribers = {}
        self._sub_last_msg = {}
        self._subscribe_lock = threading.Lock()

        self._watchers = {}
        self._watch_values = {}

        self._last_console_print = ""
        self._console_print_lines = []
        self.subscribe("output/tty", self._console_listener)

    def _console_listener(self, msg):
        if msg.body.endswith("> \n"):
            self._last_console_print = "".join(self._console_print_lines)
            self._console_print_lines = []
        else:
            self._console_print_lines.append(msg.body)

    def _listener_thread(self):
        while True:
            msg = self.sock.recv_spec()
            print("MSG LOG", msg)
            if msg.cmd == EventTypes.SV_EVENT:
                if msg.name in self._subscribers.keys():
                    self._sub_last_msg[msg.name] = msg
                    for cb in self._subscribers[msg.name]:
                        threading.Thread(target=cb, args=(msg,)).start()

            if msg.sn > 0 and msg.sn in self._sn_callbacks.keys():
                threading.Thread(target=self._sn_callbacks[msg.sn], args=(msg,)).start()
                if msg.sn in self._reply_events:
                    self._reply_events[msg.sn].set()
                    self._reply_msgs = msg
                del self._sn_callbacks[msg.sn]

    def _send(self, command:str, data_type:int=0, property_name:str="", body:bytes=b'', error:bool=False, flags:List[int]=[], rows:int=0, cols:int=0, wait_for_response:float=0, callback:Callable[SpecMessage, None]=None) -> None:
        """
        Send a message to SPEC

        Properties:
            wait_for_reponse (float): The number of seconds to wait for a response before returning

        Returns:
            (SpecMessage): Reply from SPEC if it occurred within wait_for_response seconds
        """
        with self._send_lock:
            self._sn_counter = self._sn_counter + 1
            if callback is not None:
                self._sn_callbacks[self._sn_counter] = callback
            if wait_for_response != 0:
                self._reply_events[self._sn_counter] = threading.Event()
            self.sock.send_spec(self._sn_counter, command, data_type, property_name, body, error, flags, rows, cols)
            if wait_for_response != 0:
                self._reply_events[self._sn_counter].wait(wait_for_response)
                msg = self._reply_msgs[self._sn_counter]
                del self._reply_events[self._sn_counter]
                del self._reply_msgs[self._sn_counter]
                return msg


    def subscribe(self, prop:str, callback:Callable[SpecMessage, None], nowait:bool=False, timeout:float=0.1) -> bool:
        """
        Subscribe to changes in a property.

        Parameters:
            prop (string): The name of the property ("*" for all)
            callback (function): The function to be called when the event is received. Will also be called immediately after subscribing
            nowait (boolean): By default the function waits for the first event after registering to see if an error occurred. To skip that set True
            timeout (float): The timeout to wait for a response after subscribing. Function returns False when it runs out 

        Returns:
            True if successful, False when timeout reached
        """
        with self._subscribe_lock:
            if not prop in self._subscribers.keys(): # Not registered with SPEC yet
                if not nowait: # Actually wait and see if it was successful

                    # Subscribe to both error and the property to see what happens first
                    last_msg = {"event": threading.Event(), "msg": None}

                    def last_msg_cb(msg):
                        last_msg["msg"] = msg
                        last_msg["event"].set()
                    
                    self._subscribers[prop] = [last_msg_cb]
                    self._subscribers["error"].append(last_msg_cb)

                    self._send(EventTypes.SV_REGISTER, property_name=prop)

                    if not last_msg["event"].wait(timeout): # Timeout ran out
                        del self._subscribers[prop]
                        return False
                    if last_msg["msg"].cmd == EventTypes.SV_EVENT and last_msg["msg"].name == "error": # The last message was an error => subscribing didn't work
                        del self._subscribers[prop]
                        raise SpecError(last_msg["msg"].body)

                    threading.Thread(target=callback, args=(last_msg["msg"],)).start() # Forward the function to the callback since it was successful
                    self._subscribers[prop] = [callback]
                    self._subscribers["error"].remove(last_msg_cb) # cleanup
                else:
                    self._subscribers[prop] = [callback]
                    self._send(EventTypes.SV_REGISTER, property_name=prop)
            else:
                threading.Thread(target=callback, args=(self._sub_last_msg[prop],)).start() # Call function with latest value
                self._subscribers[prop].append(callback)

            return True

    def unsubscribe(self, prop:str, callback:Callable[SpecMessage, None]):
        """
        Unsubscribe from changes in the property.

        Parameters:
            prop (string): To property to unsubscribe from
            callback (function): The callback function

        Returns:
            (boolean): True if the callback was removed, False if it didn't exist anyways
        """
        with self._subscribe_lock:
            if prop in self._subscribers and callback in self._subscribers[prop]: 
                self._subscribers[prop].remove(callback)

                # Unsubscribe if nothing is listening anymore
                if len(self._subscribers[prop]) == 0:
                    self._send(EventTypes.SV_UNREGISTER, property_name=prop)
                    del self._subscribers[prop]
                return True
            return False

    def run(self, console_command:str, blocking:bool=True, callback:Callable[[SpecMessage, str], None]=None) -> [SpecMessage, str]:
        """
        Execute a command like from the interactive SPEC console

        Arguments:
            console_command (string): The command to execute
            blocking (boolean): When True, the function will block until it receives a response from SPEC and return the response
            callback (function): When blocking=False, the response will instead be send to the callback function. Expected to accept 2 positional arguments: data, console_output
        
        Returns:
            [Message, string]: If blocking, the response message from the server and what would be printed to console
        """
        event = EventTypes.SV_FUNC_WITH_RETURN if blocking or callback is not None else EventTypes.SV_FUNC
        if console_command[-1] != '\n':
            console_command += '\n'
        
        if blocking or callback:
            res = {"event": threading.Event(), "val": None}
            def res_cb(msg):
                if callback:
                    threading.Thread(target=callback, args=(msg, self._last_console_print)).start()
                if blocking:
                    res["val"] = msg
                    res["event"].set()
            
            self._send(event, property_name=console_command, callback=res_cb)

            if blocking:
                res["event"].wait()
                return res["val"], self._last_console_print
        else:
            self._send(event, property_name=console_command)

    def set(self, prop, value, wait_for_error=0.1):
        """
        Set a property.

        Attributes:
            prop_name (string): The name of the property
            value: The value (will be converted to datatype before sending)
            wait_for_error (float): SPEC only sends a message back if the property doesn't exist. Set the number of seconds to wait for an error message (if there is one)
        """
        res = self._send(EventTypes.SV_CHAN_SEND, DataTypes.SV_STRING, property_name=prop, body=value.encode('ascii'), wait_for_response=0.1)
        if res and res.type == DataTypes.SV_ERROR:
            raise SpecError(res.body)
        if prop in self._watch_values:
            self._watch_values[prop]["body"] = value.encode("ascii")

    def get(self, prop):
        """
        Get a property.

        Attributes:
            prop_name (string): The name of the property

        Returns:
            None if property doesn't exist
        """
        if prop in self._watch_values:
            return self._watch_values[prop]
        return self._send(EventTypes.SV_CHAN_READ, DataTypes.SV_STRING, property_name=prop, wait_for_response=0.5)

    def watch(self, prop):
        """
        Listen for changes in prop to speed up .get() method

        Parameters:
            prop (string): Name of the prop to watch

        Returns:
            (boolean): True if successful
        """
        def watcher(msg):
            self._watch_values[prop] = msg
        self._watchers[prop] = watcher
        return self.subscribe(prop, watcher)

    def unwatch(self, prop):
        """
        Stop listening for changes in prop

        Parameters:
            prop (string): Name of the prop to stop watching
        """
        self.unsubscribe(prop, self._watchers[prop])
        del self._watchers[prop]
        del self._watch_values[prop]

    def motor(self, mne):
        """
        Get the motor as an object

        Parameters:
            mne (string): The mnemonic name of the motor

        Returns:
            (Motor): The motor
        """
        return Motor(mne, self)

    def var(self, name, dtype=str):
        """
        Get the variable as an object

        Parameters:
            name (string): The name of the variable
            dtype (Type): The type of the variable

        Returns:
            (Var): The variable
        """
        val = self.get("var/{}".format(name))
        if val and val.type in DataTypes.ARRAYS:
            return ArrayVar(name, self)
        return Var(name, self, dtype=dtype)
    
    def abort(self):
        """
        Abort all running commands
        """
        self._send(EventTypes.SV_ABORT)

    @property
    def motors(self):
        """
        Dict of all available motor mnemonic and pretty names
        """
        motors = collections.OrderedDict()
        ms = self.var("A").value
        for m in ms.keys():
            motors[self.run("motor_mne({})".format(m))[0].body] = self.run("motor_name({})".format(m))[0].body
        return motors

    def _get_counter_names(self):
        """
        Refresh the counter names from the server
        """
        self.counter_names = collections.OrderedDict()
        for i in range(self.var("COUNTERS", dtype=int).value):
            self.counter_names[self.run("cnt_mne({})".format(i))[0].body] = self.run("cnt_name({})".format(i))[0].body 
        return self.counter_names      

    def count(self, time, callback=None, refresh_names=False):
        """
        Counts scalers for the time specified. This function is blocking. The callback function will receive occasional updates during counting and when counting is finished.

        Parameters:
            time (float): The time to count in seconds
            callback (function): Callback function for updates during counting
            refresh_names (boolean): If True, counter names will be refreshed before starting to count. Only necessary if a counter has been added or removed since the script started.

        Returns:
            (OrderedDict): Counter values
        """
        if refresh_names:
            self._get_counter_names()

        countvals = {key: 0.0 for key in self.counter_names.keys()}
        def count_callback(res):
            countvals[res.name.split("/")[1]] = float(res.body)
            if callback:
                threading.Thread(target=callback, args=(countvals,)).start()
        
        for counter in self.counter_names.keys():
            self.subscribe("scaler/{}/value".format(counter), count_callback)

        self.run("count {}".format(time))

        for counter in self.counter_names.keys():
            count_callback(self.get("scaler/{}/value".format(counter))) # Ensure that the final values are read. It says in the docs the callback does it, but it didn't seem reliable
            self.unsubscribe("scaler/{}/value".format(counter), count_callback)

        return countvals

    def stop_counting(self):
        """
        Stop counting immediately. Will also cause .count() call to return if started in different thread.
        """
        self.set("scaler/.all./count", 0)
