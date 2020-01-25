"""
    jupylet/state.py
    
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


import hashlib
import random
import pickle


def o2h(o, n=12):
    return hashlib.sha256(pickle.dumps(o)).hexdigest()[:n]


def save_state(name, path, *args):
    
    if not path:
        path = '%s-%s.state' % (name, o2h(random.random()))

    with open(path, 'wb') as f:
        sl = [o.get_state() for o in args]
        pickle.dump(sl, f)
        
    return path

        
def load_state(path, *args):
    
    with open(path, 'rb') as f:
        sl = pickle.load(f)
        for o, s in zip(args, sl):
            o.set_state(s)
            
            
class State(object):
    
    def __init__(self, **kwargs):
        
        for k, v in kwargs.items():
            setattr(self, k, v)
            
    def __repr__(self):
        return repr(self.__dict__)
    
    def __setitem__(self, key, item):
        self.__dict__[key] = item

    def __getitem__(self, key):
        return self.__dict__[key]

    def get_state(self):
        return self

    def set_state(self, s):
        self.__dict__ = vars(s)
