import array
import micropython
import watch


@micropython.viper
def _compare(d1, d2, count: int, shift: int) -> int:
    """Compare two sequences of (signed) bytes and quantify how dissimilar
    they are.
    """
    p1 = ptr8(d1)
    p2 = ptr8(d2)

    e = 0
    for i in range(count):
        s1 = int(p1[i])
        if s1 > 127:
            s1 -= 256

        s2 = int(p2[i])
        if s2 > 127:
            s2 -= 256

        d = s1 - s2
        e += d * d
    return e


class Biquad:
    """Direct Form II Biquad Filter"""

    def __init__(self, b0, b1, b2, a1, a2):
        self._coeff = (b0, b1, b2, a1, a2)
        self._v1 = 0
        self._v2 = 0

    def step(self, x):
        c = self._coeff
        v1 = self._v1
        v2 = self._v2

        v = x - (c[3] * v1) - (c[4] * v2)
        y = (c[0] * v) + (c[1] * v1) + (c[2] * v2)

        self._v2 = v1
        self._v1 = v
        return y


class PTAGC:
    """Peak Tracking Automatic Gain Control
    In order for the correlation checks to work correctly we must
    aggressively reject spikes caused by fast DC steps. Setting a
    threshold based on the median is very effective at killing
    spikes but needs an extra 1k for sample storage which isn't
    really plausible for a microcontroller.
    """

    def __init__(self, start, decay, threshold):
        self._peak = start
        self._decay = decay
        self._boost = 1 / decay
        self._threshold = threshold

    def step(self, spl):
        # peak tracking
        peak = self._peak
        if abs(spl) > peak:
            peak *= self._boost
        else:
            peak *= self._decay
        self._peak = peak

        # rejection filter (clipper)
        threshold = self._threshold
        if spl > (peak * threshold) or spl < (peak * -threshold):
            return 0

        # booster
        spl = 100 * spl / (2 * peak)

        return spl


class PPG:
    """ """

    def __init__(self, spl):
        self._offset = spl
        self.data = array.array("b")
        self.debug = None

        self._hpf = Biquad(0.87033078, -1.74066156, 0.87033078, -1.72377617, 0.75754694)
        self._agc = PTAGC(20, 0.971, 2)
        self._lpf = Biquad(0.11595249, 0.23190498, 0.11595249, -0.72168143, 0.18549138)

    def preprocess(self, spl):
        """Preprocess a PPG sample.
        Must be called at 24Hz for accurate heart rate calculations.
        """
        if self.debug != None:
            self.debug.append(spl)
        spl -= self._offset
        spl = self._hpf.step(spl)
        spl = self._agc.step(spl)
        spl = self._lpf.step(spl)
        spl = int(spl)

        self.data.append(spl)
        return spl

    def _get_heart_rate(self):
        def compare(d, shift):
            return _compare(d[shift:], d[:-shift], len(d) - shift, shift)

        def trough(d, mn, mx):
            z2 = compare(d, mn - 2)
            z1 = compare(d, mn - 1)
            for i in range(mn, mx + 1):
                z = compare(d, i)
                if z2 > z1 and z1 < z:
                    return i
                z2 = z1
                z1 = z

            return -1

        data = memoryview(self.data)

        # Search initially from ~210 to 30 bpm
        t0 = trough(data, 7, 48)
        if t0 < 0:
            return None

        # Check the second cycle ...
        t1 = t0 * 2
        t1 = trough(data, t1 - 5, t1 + 5)
        if t1 < 0:
            return None

        # ... and the third
        t2 = (t1 * 3) // 2
        t2 = trough(data, t2 - 5, t2 + 4)
        if t2 < 0:
            return None

        # If we can find a fourth cycle then use that for the extra
        # precision otherwise report whatever we've found
        t3 = (t2 * 4) // 3
        t3 = trough(data, t3 - 4, t3 + 4)
        if t3 < 0:
            return (60 * 24 * 3) // t2
        return (60 * 24 * 4) // t3

    def get_heart_rate(self):
        if len(self.data) < 200:
            return None

        hr = self._get_heart_rate()

        # Clear out the accumulated data
        self.data = array.array("b")

        # Dump the debug data
        if self.debug:
            with open("hrs.data", "ab") as f:
                # Re-sync marker
                f.write(b"\xff\xff")
                now = watch.rtc.get_localtime()
                f.write(array.array("H", now[:6]))
                f.write(self.debug)
            self.debug = array.array("H")

        return hr

    def enable_debug(self):
        if self.debug == None:
            self.debug = array.array("H")


"""Heart rate monitor
~~~~~~~~~~~~~~~~~~~~~
A graphing heart rate monitor using a PPG sensor.
.. figure:: res/HeartApp.png
    :width: 179
This program also implements some (entirely optional) debug features to
store the raw heart data to the filesystem so that the samples can be used
to further refine the heart rate detection algorithm.
To enable the logging feature select the heart rate application using the
watch UI and then run the following command via wasptool:
.. code-block:: sh
    ./tools/wasptool --eval 'wasp.system.app.debug = True'
Once debug has been enabled then the watch will automatically log heart
rate data whenever the heart rate application is running (and only
when it is running). Setting the debug flag to False will disable the
logging when the heart rate monitor next exits.
Finally to download the logs for analysis try:
.. code-block:: sh
    ./tools/wasptool --pull hrs.data
"""

import wasp
import machine
# import ppg


class HeartApp:
    """Heart rate monitor application."""

    NAME = "Heart"

    def __init__(self):
        self._debug = False
        self._hrdata = None

    def foreground(self):
        """Activate the application."""
        wasp.watch.hrs.enable()

        # There is no delay after the enable because the redraw should
        # take long enough it is not needed
        draw = wasp.watch.drawable
        draw.fill()
        draw.set_color(wasp.system.theme("bright"))
        draw.string("PPG graph", 0, 6, width=240)

        wasp.system.request_tick(1000 // 8)

        self._hrdata = ppg.PPG(wasp.watch.hrs.read_hrs())
        if self._debug:
            self._hrdata.enable_debug()
        self._x = 0

    def background(self):
        wasp.watch.hrs.disable()
        self._hrdata = None

    def _subtick(self, ticks):
        """Notify the application that its periodic tick is due."""
        draw = wasp.watch.drawable

        spl = self._hrdata.preprocess(wasp.watch.hrs.read_hrs())

        if len(self._hrdata.data) >= 240:
            draw.set_color(wasp.system.theme("bright"))
            draw.string("{} bpm".format(self._hrdata.get_heart_rate()), 0, 6, width=240)

        # Graph is orange by default...
        color = wasp.system.theme("spot1")

        # If the maths goes wrong lets show it in the chart!
        if spl > 100 or spl < -100:
            color = 0xFFFF
        if spl > 104 or spl < -104:
            spl = 0
        spl += 104

        x = self._x
        draw.fill(0, x, 32, 1, 208 - spl)
        draw.fill(color, x, 239 - spl, 1, spl)
        if x < 238:
            draw.fill(0, x + 1, 32, 2, 208)
        x += 2
        if x >= 240:
            x = 0
        self._x = x

    def tick(self, ticks):
        """This is an outrageous hack but, at present, the RTC can only
        wake us up every 125ms so we implement sub-ticks using a regular
        timer to ensure we can read the sensor at 24Hz.
        """
        t = machine.Timer(id=1, period=8000000)
        t.start()
        self._subtick(1)
        wasp.system.keep_awake()

        while t.time() < 41666:
            pass
        self._subtick(1)

        while t.time() < 83332:
            pass
        self._subtick(1)

        t.stop()
        del t

    @property
    def debug(self):
        return self._debug

    @debug.setter
    def debug(self, value):
        self._debug = value
        if value and self._hrdata:
            self._hrdata.enable_debug()
