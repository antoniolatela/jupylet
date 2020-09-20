"""
    jupylet/sound.py
    
    Copyright (c) 2020, Nir Aides - nir@winpdb.org

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice, this
       list of conditions and the following disclaimer.
    2. Redistributions in binary form must reproduce the above copyright notice,
       this list of conditions and the following disclaimer in the documentation
       and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
    ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
    WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
    DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
    ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
    (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
    LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
    ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
    (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
    SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


import functools
import _thread
import logging
import random
import queue
import copy
import math
import time
import sys
import os
import re

import skimage.draw
import scipy.signal
import PIL.Image

try:
    import sounddevice as sd
except:
    sd = None

import soundfile as sf
import numpy as np

from scipy import signal

from .resource import find_path
from .utils import o2h, callerframe, trimmed_traceback, auto
from .utils import setup_basic_logging, get_logging_level
from .env import get_app_mode, is_remote


logger = logging.getLogger(__name__)


DEBUG = False

EPSILON = 1e-6

FPS = 44100


def t2frames(t):
    """Convert time in seconds to frames at 44100 frames per second.
    
    Args:
        t (float): The time duration in seconds.

    Returns:
        int: The number of frames.
    """
    return int(FPS * t)


def frames2t(frames):
    """Convert frames at 44100 frames per second to time in seconds.
    
    Args:
        frames (int): The number of frames.

    Returns:
        float: The time duration in seconds.
    """
    return frames  / FPS
    

_worker_tid = None


def _init_worker_thread():
    logger.info('Enter _init_worker_thread().')

    global _worker_tid
    if not _worker_tid:
        _worker_tid = _thread.start_new_thread(_start_sound_stream, ())
        
#
# This queue is only used to keep the sound stream running.
#
_workerq = queue.Queue()


def _start_sound_stream():
    """Start the sound device output stream handler."""
    logger.info('Enter _start_sound_stream().')
    
    #
    # Latency is set to 66ms since on Windows it appears to invoke the callback 
    # regularly at nearly 10ms which is the effective resolution of sleep 
    # "wakeups" on Windows.
    # 
    # The default 'low' latency appears to be 100ms (which is a little high)
    # and alternates between calling the callback at 10ms and 20ms intervals.
    # 
    # Such alternation would cause (ear) noticable delays with starting the 
    # playing of repeating sounds. This problem could and should be fixed by 
    # adding a mechanism to schedule the start of new sounds to a particular 
    # frame in the output stream buffer.
    #
    with sd.OutputStream(channels=2, callback=_stream_callback, latency='low'):#0.066):
        _workerq.get()
        
    global _worker_tid
    _worker_tid = None
    

_al = []
_dt = []


def _stream_callback(outdata, frames, _time, status):
    """Compute and set the sound data to be played in a few milliseconds.

    Args:
        outdata (ndarray): The sound device output buffer.
        frames (int): The number of frames to compute and set into the output
            buffer.
        _time (struct): A bunch of clocks.
    """
    t0 = time.time()

    if status:
        logger.warning('Stream callback called with status: %r.', status)
    
    #
    # Get all sound objects currently playing, mix them, and set the mixed
    # array into the output buffer.
    #

    sounds = _get_sounds()
    
    if not sounds:
        a0 = np.zeros_like(outdata)
        outdata[:] = a0

        #_workerq.put('QUIT')
        #raise sd.CallbackStop
        
    else: 
        a0 = _mix_sounds(sounds, frames)
        outdata[:] = a0

    # 
    # Aggregate the output data and timers for the oscilloscope.
    #

    if len(_al) * frames > FPS:
        _al.pop(0)
        _dt.pop(0)

    _al.append(a0)
    _dt.append((
        t0, 
        frames, 
        _time.inputBufferAdcTime,
        _time.outputBufferDacTime,
        _time.currentTime,
    ))


#
# A set of all currently playing sound objects.
#
_sounds = set()


def _add_sound(sound):
    """Add sound to the set of currently playing sound objects."""

    if sd is None:
        return

    if _worker_tid is None:
        _init_worker_thread()

    _sounds.add(sound)

         
def _get_sounds():
    """Get currently playing sound objects."""

    for s in list(_sounds):
        if s.done:
            _sounds.discard(s)

    return list(_sounds)
      

def _mix_sounds(sounds, frames):
    """Mix sound data from given sounds into a single numpy array.

    Args:
        sounds: Currently playing sound objects.
        frames (int): Number of frames to consume from each sound object.

    Returns:
        ndarray
    """
    d = np.stack([s._consume(frames) for s in sounds])
    return np.sum(d, 0).clip(-1, 1)


def get_oscilloscope_as_image(
    fps,
    ms=256, 
    amp=1., 
    color=255, 
    size=(512, 256),
    scale=2.
):
    """Get an oscilloscope image of sound data sent to the sound device.

    Args:
        fps (float): frame rate (in frames per second) of the monitor on which 
            the oscilloscope is to be displayed. The given rate will affect the
            visual stability of the displayed wave forms.
        ms (float): Span of the oscilloscope x-axis in milliseconds.
        amp (float): Scale of the y-axis (amplification of the signal).
        color (int or tuple): Color of the drawn sound wave.
        size (tuple): size of oscilloscope in pixels given as (width, height).

    Returns:
        Image, float, float: a 3-tuple with the oscilloscope image, and the 
            timestamps of the leftmost and rightmost drawn samples.
    """
    w0, h0 = size
    w1, h1 = int(w0 // scale), int(h0 // scale)

    a0, ts, te = get_oscilloscope_as_array(fps, ms, amp, color, (w1, h1))
    im = PIL.Image.fromarray(a0).resize(size).transpose(PIL.Image.FLIP_TOP_BOTTOM)

    return im, ts, te


def get_oscilloscope_as_array(
    fps,
    ms=256, 
    amp=1., 
    color=255, 
    size=(512, 256)
):
    """Get an oscilloscope array of sound data sent to the sound device.

    Args:
        fps (float): frame rate (in frames per second) of the monitor on which 
            the oscilloscope is to be displayed. The given rate will affect the
            visual stability of the displayed wave forms.
        ms (float): Span of the oscilloscope x-axis in milliseconds.
        amp (float): Scale of the y-axis (amplification of the signal).
        color (int or tuple): Color of the drawn sound wave.
        size (tuple): size of oscilloscope in pixels given as (width, height).

    Returns:
        ndarray, float, float: a 3-tuple with the oscilloscope array, and the 
            timestamps of the leftmost and rightmost drawn samples.        
    """
    
    ms = min(ms, 256)
    w0, h0 = size

    oa = get_output_as_array()
    if not oa or len(oa[0]) < 1000:
        return np.zeros((h0, w0, 4), dtype='uint8'), 0, 0

    a0, tstart, tend = oa

    t0 = int((time.time() + 0.01) * fps) / fps
    te = t0 + min(0.025, ms / 2000)
    ts = te - ms / 1000

    s1 = int((te - tend) * FPS)
    s0 = int((ts - tend) * FPS)

    a0 = a0[s0: s1] * amp
    a0 = a0.clip(-1., 1.)

    if len(a0) < 100:
        return np.zeros((h0, w0, 4), dtype='uint8'), 0, 0

    a0 = scipy.signal.resample(a0, w0)

    a1 = np.arange(len(a0))
    a2 = ((a0 + 1) * h0 / 2).clip(0, h0 - 1).astype(a1.dtype)
    a3 = np.stack((a2, a1), -1)

    a4 = np.concatenate((a3[:-1], a3[1:]), -1)
    a5 = np.zeros((h0, w0, 4), dtype='uint8')
    a5[...,:3] = color

    for cc in a4:
        y, x, c = skimage.draw.line_aa(*cc)
        a5[y, x, -1] = c * 255
    
    return a5, ts, te


def get_output_as_array(start=-FPS, end=None, resample=None):

    if not _dt or not _al:
        return

    t0, st, _, da, ct = _dt[-1]
    t1 = t0 + da - ct + st / FPS

    a0 = np.concatenate(_al).mean(-1)[start:end]
    
    tend = t1 + (end or 0) / FPS
    tstart = tend - len(a0) / FPS

    if resample:
        a0 = scipy.signal.resample(a0, resample)
    
    return a0, tstart, tend


'''
    def play_release(self, release=None):

        if release is not None:
            self.release = release
            
        self.duration = frames2t(self.index)
        
    def play_new(self, note=None, **kwargs):
        """Play new copy of sound.

        If sound is already playing it will play the new copy in parallel. 
        This function returns the new sound object.
        """
        return self.copy().play(note, **kwargs)

    def play(self, note=None, **kwargs):
        logger.info('Enter Sample.play(note=%r, **kwargs=%r).', note, kwargs)
        
        if note is not None:
            self.note = note

        for k, v in kwargs.items():
            if k in self.__dict__:
                setattr(self, k, v)

        self.load()

        #
        # Set the reset flag to avoid synchronization / consistency problems
        # with the playback thread.
        #
        if self.playing:  
            self.reset = True
            return self

        self.playing = True
        _soundsq.put(self)
        return self
            
    def copy(self, **kwargs):

        o = copy.copy(self)

        o.playing = False
        o.reset = False
        o._stop = False
        o.index = 0
        
        for k, v in kwargs.items():
            if k in o.__dict__:
                setattr(o, k, v)

        return o
'''


dtd = {}
syd = {}


def use(synth, **kwargs):

    if kwargs:
        synth = synth.copy(**kwargs)

    cf = hash(callerframe())
    syd[cf] = synth


def play(note, **kwargs):

    cf = hash(callerframe())
    sy = syd[cf]

    return sy.play_new(note, **kwargs)


def sleep(dt=0):
    
    dt = max(0, dt)
    cf = hash(callerframe())

    tt = time.time()
    t0 = dtd.get(cf) or tt
    t1 = dtd[cf] = max(t0 + dt, tt)
    
    return asyncio.sleep(t1 - tt)


#--------------------------------------------------


def _expand_channels(a0, channels):

    if len(a0.shape) == 1:
        a0 = np.expand_dims(a0, -1)

    if a0.shape[1] < channels:
        a0 = a0.repeat(channels, 1)

    if a0.shape[1] > channels:
        a0 = a0[:,:channels]

    return a0


#@functools.lru_cache(maxsize=1024)
def _ampan(amp, pan):
    return np.array([1 - pan, 1 + pan]) * (amp / 2)


_LOG_C4 = math.log(262)
_LOG_CC = math.log(2) / 12
_LOG_CX = _LOG_C4 - 60 * _LOG_CC


def key2freq(key):
    
    if isinstance(key, np.ndarray):
        return np.exp(key * _LOG_CC + _LOG_CX)
    else:
        return math.exp(key * _LOG_CC + _LOG_CX)
    
 
def freq2key(freq):
    
    if isinstance(freq, np.ndarray):
        return (np.log(freq) - _LOG_CX) / _LOG_CC
    else:
        return (math.log(freq) - _LOG_CX) / _LOG_CC
        

_notes = dict(
    C = 1,
    Cs = 2, Db = 2,
    D = 3,
    Ds = 4, Eb = 4,
    E = 5,
    F = 6,
    Fs = 7, Gb = 7,
    G = 8, 
    Gs = 9, Ab = 9,
    A = 10,
    As = 11, Bb = 11,
    B = 12,
)


class note(object):
    pass


for o in range(8):
    for k in _notes:
        ko = (k + str(o)).rstrip('0')
        setattr(note, ko, ko)


def note2key(note):
    
    if note[-1].isdigit():
        octave = int(note[-1])
        note = note[:-1]
    else:
        octave = 4
        
    note = note.replace('#', 's')
    return _notes[note] + octave * 12 + 11


def key2note(key):
    """Convert keyboard key to note. e.g. 60 to 'C4'.

    Args:
        key (float): keyboard key to convert.

    Returns:
        str: A string representing the note. In the conversion process
            the floating point key will be rounded in a special way that 
            preserves the nearest note to the key. e.g. 60.9 and 61.1 
            will converted to Cs4, Db4 respectively.
    """ 
    i = (key - 11) % 12 

    octave = (round(key) - 12) // 12

    n0, i0 = 'B', 0

    for n1, i1 in _notes.items():
        if i <= i1:
            break

        n0, i0 = n1, i1
    
    note = n0 if i1 - i > 0.5 else n1
    
    return note + str(int(octave))


class Sound(object):
    """The base class for all other sound classes, including audio samples, 
    oscillators and effects.
    """

    def __init__(self):
        
        self.freq = 262.
        
        # Amplitude (or volume) beween 0 and 1.
        self.amp = 1.
        
        # Left-right audio balance - a value between -1 and 1.
        self.pan = 0.
        
        # The number of frames that the forward() method must return.
        self.frames = 1024

        # The frame counter.
        self.index = 0
        
        # The most last frame array returned by the forward() function.
        self._a0 = None
        self._al = []
                
    def _rset(self, key, value, force=False):
        
        if force or self.__dict__.get(key, '__NONE__') != value:
            for s in self.__dict__.values():
                if isinstance(s, Sound):
                    s._rset(key, value, force=True)
            
        self.__dict__[key] = value
    
    def _ccall(self, name, *args, **kwargs):
        
        for s in self.__dict__.values():
            if isinstance(s, Sound):
                getattr(s, name)(*args, **kwargs)
                
    def play_release(self, release=None):
        """
        if release is not None:
            self.release = release
            
        self.duration = frames2t(self.index)
        """
        pass
        
    def play_new(self, note=None, **kwargs):
        """Play new copy of sound.

        If sound is already playing it will play the new copy in parallel. 
        This function returns the new sound object.
        """
        o = self.copy()
        o.play(note, **kwargs)

        return o

    def play(self, note=None, **kwargs):
        #logger.info('Enter Sample.play(note=%r, **kwargs=%r).', note, kwargs)
        
        self.reset()
        
        if note is not None:
            self.note = note

        for k, v in kwargs.items():
            if k in self.__dict__:
                setattr(self, k, v)
              
        _add_sound(self)
                
    def copy(self):
        
        o = copy.copy(self)
        
        for k, v in o.__dict__.items():
            if isinstance(v, Sound):
                setattr(o, k, v.copy())
                
        return o
       
    def reset(self):
        
        self.index = 0
        self._a0 = None
        
        self._ccall('reset')
        
    def _consume(self, frames, channels=2, *args, **kwargs):
        
        self._rset('frames', frames)
        
        a0 = self(*args, **kwargs)
        a0 = _expand_channels(a0, channels)
        
        if channels == 2:
            return a0 * _ampan(self.amp, self.pan)
        
        return a0 * self.amp

    @property
    def done(self):
        
        if self.index < FPS / 8:
            return False
        
        if self._a0 is None:
            return False
        
        return np.abs(self._a0).max() < EPSILON
        
    def __call__(self, *args, **kwargs):
        
        assert getattr(self, 'frames', None) is not None, 'You must call super() from your sound class constructor'
        
        self._a0 = self.forward(*args, **kwargs)
        self.index += self.frames
        
        if DEBUG and isinstance(self._a0, np.ndarray):
            self._al = self._al[-255:] + [self._a0]

        return self._a0

    @property
    def _a1(self):
        return np.concatenate(self._al)

    def forward(self, *args, **kwargs):
        return np.zeros((self.frames, self.channels))
    
    @property
    def key(self):
        return freq2key(self.freq)
    
    @key.setter
    def key(self, value):
        self.freq = key2freq(value)
        
    @property
    def note(self):
        return key2note(self.key)
    
    @note.setter
    def note(self, value):
        self.key = note2key(value)


class Gate(Sound):
    
    def __init__(self):
        
        super(Gate, self).__init__()
        
        self.states = []
        
    def forward(self):
        
        states = []
        end = self.index + self.frames
        
        while self.states:
            if self.states[0][0] >= end:
                break
            states.append(self.states.pop(0))
        
        return states
        
    def open(self, t):
        self._append(t, 'open')
        
    def release(self, t):
        self._append(t, 'release')
        
    def _append(self, t, event):
        
        lasti = self.index
        if self.states:
            lasti = self.states[-1][0]

        frame = max(int(t * FPS), lasti)
        self.states.append((frame, event))


def get_exponential_adsr_curve(dt, start=0, end=None, th=0.01):
    
    df = max(math.ceil(dt * FPS), 1)
    end = min(df, end or 60 * FPS)
    start = start + 1
        
    a0 = np.arange(start/df, end/df + EPSILON, 1/df, dtype='float64')
    a1 = np.exp(a0 * math.log(th))
    a2 = (1. - a1) / (1. - th)
    
    return a2


def get_linear_adsr_curve(dt, start=0, end=None):
    
    df = max(math.ceil(dt * FPS), 1)
    end = min(df, end or 60 * FPS)
    start = start + 1
    
    a0 = np.arange(start/df, end/df + EPSILON, 1/df, dtype='float64')
    
    return a0


class Envelope(Sound):
    
    def __init__(
        self, 
        attack=0.,
        decay=0., 
        sustain=1., 
        release=0.,
        linear=True,
    ):
        
        super(Envelope, self).__init__()
        
        self.attack = attack
        self.decay = decay
        self.sustain = sustain
        self.release = release
        self.linear = linear
        
        self._state = None
        self._start = 0
        self._valu0 = 0
        self._valu1 = 0
        
    def forward(self, states):
        
        end = self.index + self.frames
        index = self.index
        states = states + [(end, 'continue')]
        curves = []
        
        for frame, event in states:
            #print(frame, event)
            
            while index < frame:
                curves.append(self.get_curve(index, frame))
                index += len(curves[-1])
                    
            if event == 'open' and self._state != 'attack':
                self._state = 'attack'
                self._start = index
                self._valu0 = self._valu1
            
            if event == 'release' and self._state not in ('release', None):
                self._state = 'release'
                self._start = index
                self._valu0 = self._valu1
            
        return np.concatenate(curves)[:,None]
    
    def get_curve(self, start, end):
        
        assert start < end
        
        if self._state in (None, 'sustain'):
            return np.ones((end - start,), dtype='float64') * self._valu0
        
        start = start - self._start
        end = end - self._start
        dt = getattr(self, self._state)
                    
        if self.linear:
            curve = get_linear_adsr_curve(dt, start, end)
        else:
            curve = get_exponential_adsr_curve(dt, start, end)
    
        #print(dt, start, end, len(curve), curve[-1])
        done = curve[-1] >= 0.9999
        
        if self._state == 'attack':
            target = 1.
            next_state = 'decay'
            
        if self._state == 'decay':
            target = self.sustain * self._valu0
            next_state = 'sustain'
            
        if self._state == 'release':
            target = 0.
            next_state = None
            
        curve = (target - self._valu0) * curve  + self._valu0
        
        if done:
            self._state = next_state
            self._start += start + len(curve)
            self._valu0 = curve[-1]
            
        self._valu1 = curve[-1]
        
        return curve


#
# Do not change this "constant"!
#
_NP_ZERO = np.zeros((1,), dtype='float64')


def get_radians(freq, start=0, frames=8192):
    
    pt = 2 * math.pi / FPS * freq
    
    if isinstance(pt, np.ndarray):
        pt = pt.reshape(-1)
    else:
        pt = pt * np.ones((frames,), dtype='float64')
            
    p0 = start + _NP_ZERO
    p1 = np.concatenate((p0, pt))
    p2 = np.cumsum(p1)
    
    radians = p2[:-1]
    next_start = p2[-1] 
    
    return radians, next_start


def get_sine_wave(freq, phase=0, frames=8192, **kwargs):
    
    radians, phase_o = get_radians(freq, phase, frames)
    
    a0 = np.sin(radians)
    
    return a0, phase_o


def get_triangle_wave(freq, phase=0, frames=8192, **kwargs):
    
    radians, phase_o = get_radians(freq, phase, frames)

    a0 = radians % (2 * math.pi)
    a1 = a0 / math.pi - 1
    a2 = a1 * np.sign(-a1)
    a3 = a2 * 2 + 1

    return a3, phase_o


def get_saw_wave(freq, phase=0, frames=8192, sign=1., **kwargs):
    
    radians, phase_o = get_radians(freq, phase, frames)

    a0 = (radians + math.pi) % (2 * math.pi) * sign / math.pi - 1
    
    return a0, phase_o


def get_pulse_wave(freq, phase=0, frames=8192, duty=0.5, **kwargs):
    
    if isinstance(duty, np.ndarray):
        duty = duty.reshape(-1)
        
    radians, phase_o = get_radians(freq, phase, frames)

    a0 = radians % (2 * math.pi) < (2 * math.pi * duty)
    a1 = a0.astype('float64') * 2. - 1.
    
    return a1, phase_o


class Oscillator(Sound):
    
    def __init__(self, shape='sine', freq=262., key=None, phase=0., sign=1, duty=0.5, **kwargs):
        
        super(Oscillator, self).__init__()
        
        self.shape = shape
        self.phase = phase
        self.freq = freq
        
        if key is not None:
            self.key = key
        
        self.sign = sign
        self.duty = duty
        self.kwargs = kwargs
        
    def forward(self, key_modulation=None, sign=None, duty=None, **kwargs):
        
        if key_modulation is not None:
            freq = key2freq(self.key + key_modulation)
        else:
            freq = self.freq
            
        if sign is None:
            sign = self.sign
            
        if duty is None:
            duty = self.duty
            
        if self.kwargs:
            kwargs = dict(kwargs)
            kwargs.update(self.kwargs)
        
        get_wave = dict(
            sine = get_sine_wave,
            tri = get_triangle_wave,
            saw = get_saw_wave,
            pulse = get_pulse_wave,
        ).get(self.shape, self.shape)
        
        a0, self.phase = get_wave(
            freq, 
            self.phase, 
            self.frames, 
            sign=self.sign,
            duty=duty, 
            **kwargs
        )
        
        return a0[:,None]


class ButterFilter(Sound):
    
    def __init__(self, order=15, freq=8192, btype='lowpass'):
        
        super(ButterFilter, self).__init__()
        
        self.order = order
        self.freq = freq
        self.btype = btype
        
        self._watch = None
        
        self.b = None
        self.a = None
        self.z = None
        
    def forward(self, x):
        
        if self._watch != (self.order, self.freq, self.btype):
            
            self._watch = (self.order, self.freq, self.btype)
            self.b, self.a = signal.butter(self.order, self.freq / FPS * 2, self.btype)
            self.z = signal.lfilter_zi(self.b, self.a)[:,None]
           
            if self.z.shape[1] != x.shape[1]:
                self.z = self.z.repeat(x.shape[1], -1)[:,:x.shape[1]]
            
            # Warmup
            self.z = signal.lfilter(self.b, self.a, x * 0, 0, zi=self.z)[-1]
            
        x, self.z = signal.lfilter(self.b, self.a, x, 0, zi=self.z)
            
        return x.astype('float64')
    

class PhaseModulator(Sound):
    
    def __init__(self, beta=1.):
        """A sort of phase modulator.
        
        It can be used for aproximate frequency modulation by using the 
        normalized cumsum of the modulated signal, but the signal should be 
        balanced so its cumsum does not drift.
        """
        
        super(PhaseModulator, self).__init__()
        
        self.beta = beta
        
        self._buffer = None
        
    def forward(self, carrier, signal):
        
        signal = signal.mean(-1).clip(-1, 1)
        beta = int(self.beta) + 1
        
        if self._buffer is None:
            self._buffer = np.zeros((2 * beta, carrier.shape[1]), dtype=carrier.dtype)
            
        t1 = np.arange(beta, beta + len(carrier), dtype='float64') + self.beta * signal
        t2 = t1.astype('int64')
        t3 = (t1 - t2.astype('float64'))[:, None]
        
        a0 = np.concatenate((self._buffer, carrier))
        a1 = a0[t2]
        a2 = a0[t2 + 1]
        a3 = a2 * t3 + a1 * (1 - t3)
        
        self._buffer = a0[-2 * beta:]
        
        return a3


_SFCACHE_THRESHOLD = 10 * FPS
_SFCACHE_SIZE = 64

_sfcache = {}


def soundfile_read(path, zero_pad=False):
    """Read sound file as a numpy array."""
    logger.info('Enter soundfile_read(path=%r).', path)

    data, fps = _sfcache.get(path, (None, None))
    
    if data is None:
    
        data, fps = sf.read(path, dtype='float64') 
        data = np.pad(data, ((0, 1), (0, 0))[:len(data.shape)])
        
        if len(data) <= _SFCACHE_THRESHOLD:

            if len(_sfcache) >= _SFCACHE_SIZE:
                _sfcache.pop(random.choice(list(_sfcache.keys())))

            _sfcache[path] = (data, fps)

    if not zero_pad:
        data = data[:-1]
        
    return data, fps


def get_indices(intervals=1, start=0, frames=8192):
        
    if isinstance(intervals, np.ndarray):
        pt = intervals.reshape(-1)
    else:
        pt = intervals * np.ones((frames,), dtype='float64')
            
    p0 = start + _NP_ZERO
    p1 = np.concatenate((p0, pt))
    p2 = np.cumsum(p1)
    
    indices = p2[:-1]
    next_start = p2[-1] 
    
    return indices, next_start


def compute_loop(indices, buff_end, loop=False, loop_start=0, loop_end=0):
    
    indices = indices.astype('int64')
    
    if not loop or loop_end <= 0:
        return indices.clip(0, buff_end-1)
    
    il = indices < loop_start
    i0 = indices * il
    i1 = ((indices - loop_start) % (loop_end - loop_start) + loop_start) * (1 - il)
    
    return i0 + i1


@functools.lru_cache(maxsize=1024)
def get_sfz_region(key, path):
    
    rl = read_sfz(path)
    
    md = 1e6
    mr = None

    for r in rl:

        if 'pitch_keycenter' not in r:
            r['pitch_keycenter'] = r['key']
            
        d = abs(key - r['pitch_keycenter'])
        if  md > d:
            md = d
            mr = r

    return mr    


@functools.lru_cache(maxsize=32)
def read_sfz(path):
    
    sfz = open(path).read()
    sfz = '\n' + re.sub(r'//.*', '', sfz)
    
    rl0 = re.findall(r'(?s)<region>.*?(?=<|$)', sfz)
    rl1 = [auto(dict(re.findall('(\w+)=(.*?(?= \w+=|\n|$))', l))) for l in rl0]
    
    return rl1


class Sample(Sound):
    
    def __init__(
        self, 
        path, 
        freq=262., 
        key=None, 
        loop=False,
    ):
        
        super(Sample, self).__init__()
        
        self.path = path
        self.buff = None
        
        self.phase = 0        
        self.freq = freq
        
        if key is not None:
            self.key = key

        self.loop = loop
        self.loop_power = None
        
        self.region = dict(
            loop_start = 0,
            loop_end = 0,
            pitch_keycenter = None,
        )
        
    def reset(self):
        
        super(Sample, self).reset()
        
        self.phase = 0
        
    def forward(self, key_modulation=None):
        
        self.load()
           
        lb = len(self.buff)
        ls = self.region.get('loop_start', 0)
        le = self.region.get('loop_end', lb - 1)
        
        pitch_keycenter = self.region['pitch_keycenter']
        
        if pitch_keycenter is None and key_modulation is None:
            
            indices, self.phase = get_indices(1., self.phase, self.frames)
            indices = compute_loop(indices, lb, self.loop, ls, le)
            
            return self.buff[indices]
          
        if pitch_keycenter is None:
            interval = key2freq(self.key + key_modulation) / self.freq
            
        elif key_modulation is None:
            interval = self.freq / key2freq(pitch_keycenter)
            
        else:
            interval = key2freq(self.key + key_modulation) / key2freq(pitch_keycenter)         
            
        indices, self.phase = get_indices(interval, self.phase, self.frames)
        
        t3 = (indices % 1.)[:, None]
        
        indices0 = compute_loop(indices, lb, self.loop, ls, le)
        indices1 = compute_loop(indices + 1, lb, self.loop, ls, le)
        
        a0 = self.buff        
        a1 = a0[indices0]
        a2 = a0[indices1]
        
        a3 = a2 * t3 + a1 * (1 - t3)
        
        lp = self.get_loop_power(indices)
        if lp is not None:
            a3 = a3 * lp[:, None]
        
        return a3

    def get_loop_power(self, indices):
        
        if self.loop_power is not None:
            
            ls = self.region['loop_start']
            le = self.region['loop_end']

            p0 = ((indices - ls) // (le - ls)).clip(0)

            return self.loop_power ** p0
        
    def load(self):
        
        if self.path.endswith('.sfz'):
            self.load_sfz()
            return self
            
        if self.buff is not None:
            return self
        
        self.buff = soundfile_read(self.path, zero_pad=True)[0]

        if len(self.buff.shape) == 1:
            self.buff = self.buff[:,None]

        if self.region['loop_end'] == 0:
            self.region['loop_end'] = len(self.buff) - 1
            
        return self
    
    def load_sfz(self):
        
        region = get_sfz_region(round(self.key), self.path)
        
        if region['pitch_keycenter'] == self.region['pitch_keycenter']:
            return
        
        path = os.path.join(os.path.dirname(self.path), region['sample'])
        
        self.buff = soundfile_read(path, zero_pad=True)[0] 
        
        if len(self.buff.shape) == 1:
            self.buff = self.buff[:,None]

        self.region = region

        if 'loop_end' in region:
            
            ls = region['loop_start']
            le = region['loop_end']

            rms0 = (self.buff[ls:(ls+le)//2] ** 2).mean() ** 0.5
            rms1 = (self.buff[(ls+le)//2:le] ** 2).mean() ** 0.5

            self.loop_power = min((rms1 / rms0) ** 2, 1.0)

        else:
            self.loop_power = None


class Synth(Sound):
    
    def __init__(self):
        
        super(Synth, self).__init__()

        self.gate0 = Gate()
        self.gate1 = Gate()
        
        self.osc0 = Oscillator('sine', 4)
        self.osc1 = Oscillator('tri')
        
        self.env0 = Envelope(0.03, 0.3, 0.7, 1., linear=False)
        self.env1 = Envelope(0., 0.8, 0., 0., linear=False)

        self.filter = ButterFilter(order=5, freq=8192, btype='lowpass')
        
    def forward(self):
        
        g0 = self.gate0()
        g1 = self.gate1()
        
        e0 = self.env0(g0)
        e1 = self.env1(g1)
        
        o0 = self.osc0()        
        o1 = self.osc1(key_modulation=o0/2+e1*2)
        
        a0 = o1 * e0
        a1 = self.filter(a0)

        return a0

    def play(self, note=None, **kwargs):
        #logger.info('Enter Synth.play(note=%r, **kwargs=%r).', note, kwargs)

        super(Synth, self).play(note, **kwargs)
        
        self.osc1.freq = self.freq
        
        self.gate0.open(0.)
        self.gate1.open(0.)
        
    def play_release(self, release=None):
        
        if release is not None:
            self.env0.release = release

        self.gate0.release(0.)

