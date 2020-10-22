from collections import OrderedDict
import time as tm

from ophyd.device import Device
from ophyd.signal import Signal
from ophyd.status import MoveStatus
from ophyd.device import Component as Cpt
from ophyd.positioner import PositionerBase
from ophyd.utils  import ReadOnlyError
try:
    from pycertifspec import Motor as SPECMotor
except:
    SPECMotor = object

class _ReadbackSignal(Signal):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._metadata.update(
            connected=True,
            write_access=False,
        )

    def get(self, **kwargs):
        self._readback = self.parent.position
        return self._readback

    def describe(self):
        res = super().describe()
        return res

    @property
    def timestamp(self):
        '''Timestamp of the readback value'''
        return tm.time()

    def put(self, value, *, timestamp=None, force=False):
        raise ReadOnlyError("The signal {} is readonly.".format(self.name))

    def set(self, value, *, timestamp=None, force=False):
        raise ReadOnlyError("The signal {} is readonly.".format(self.name))


class _SetpointSignal(Signal):
    def put(self, value, *, timestamp=None, force=False):
        self._readback = value
        self.parent.set(float(value))

    def get(self):
        return self._readback

    def describe(self):
        res = super().describe()
        return res

    @property
    def timestamp(self):
        '''Timestamp of the readback value'''
        return tm.time()

class Motor(Device, PositionerBase):
    """
    Class representing a SPEC motor that can be used with bluesky
    """

    readback = Cpt(_ReadbackSignal, value=0, kind='hinted')

    SUB_READBACK = 'readback'
    _default_sub = SUB_READBACK

    def __init__(self, motor, delay=0, precision=3, egu='', *args, **kwargs):
        """
        Create a bluesky motor from a SPEC motor

        Parameters:
            motor (pycertifspec.Motor): A pycertifspec motor
        """

        if isinstance(motor, SPECMotor):
            self.motor = motor
        else:
            raise ValueError("Motor not pycertifspec.Motor")

        self.name = self.motor.name

        super(Motor, self).__init__(*args, name=self.name, **kwargs)
        self.delay = delay
        self.precision = precision
        self.readback.name = self.name
        self.motor.add_callback(self._pos_changed)
        self._egu = egu

        # position initialize
        self._pos_changed()

    def stop(self, *args, **kwargs):
        """
        Stop all running SPEC commands if still moving
        """
        if not self.motor.move_done:
            self.motor.conn.abort()

    def set(self, position):
        self.status = MoveStatus(self, position)
        self.motor.moveto(position, blocking=False, callback=self.status.set_finished)

        return self.status

    @property
    def moving(self):
        '''Whether or not the motor is moving

        Returns
        -------
        moving : bool
        '''
        return not self.motor.move_done

    @property
    def position(self):
        return self._position

    def _pos_changed(self):
        self._set_position(self.motor.position)

    @property
    def egu(self):
        '''The engineering units (EGU) for a position'''
        return self._egu
