#! /usr/bin/env python3
#
#  Copyright 2018 California Institute of Technology
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# ISOFIT: Imaging Spectrometer Optimal FITting
# Author: David R Thompson, david.r.thompson@jpl.nasa.gov
#

import os
import sys
import numpy as np
import logging

from ..core.common import combos, eps, load_wavelen
from ..core.common import VectorInterpolator


### Functions ###

def spawn_rt(cmd):
    """Run a CLI command."""

    print(cmd)
    os.system(cmd)


### Classes ###

class FileExistsError(Exception):
    """FileExistsError with a message."""

    def __init__(self, message):
        super(FileExistsError, self).__init__(message)


class TabularRT:
    """A model of photon transport including the atmosphere."""

    def __init__(self, config):

        self.wl, self.fwhm = load_wavelen(config['wavelength_file'])
        self.n_chan = len(self.wl)

        defaults = {
            'configure_and_exit': False,
            'auto_rebuild': True
        }

        for key, value in defaults.items():
            if key in config:
                setattr(self, key, config[key])
            else:
                setattr(self, key, value)

        self.lut_grid_config = config['lut_grid']
        self.lut_dir = config['lut_path']
        self.statevec = list(config['statevector'].keys())
        self.n_point = len(self.lut_grid_config)
        self.n_state = len(self.statevec)

        self.luts = {}

        # Retrieved variables.  We establish scaling, bounds, and
        # initial guesses for each state vector element.  The state
        # vector elements are all free parameters in the RT lookup table,
        # and they all have associated dimensions in the LUT grid.
        self.bounds, self.scale, self.init = [], [], []
        self.prior_mean, self.prior_sigma = [], []
        for key in self.statevec:
            element = config['statevector'][key]
            self.bounds.append(element['bounds'])
            self.scale.append(element['scale'])
            self.init.append(element['init'])
            self.prior_sigma.append(element['prior_sigma'])
            self.prior_mean.append(element['prior_mean'])
        self.bounds = np.array(self.bounds)
        self.scale = np.array(self.scale)
        self.init = np.array(self.init)
        self.prior_mean = np.array(self.prior_mean)
        self.prior_sigma = np.array(self.prior_sigma)

    def build_lut(self, rebuild=False):
        """Each LUT is associated with a source directory.  We build a lookup table by: 
              (1) defining the LUT dimensions, state vector names, and the grid 
                  of values; 
              (2) running modtran if needed, with each MODTRAN run defining a 
                  different point in the LUT; and 
              (3) loading the LUTs, one per key atmospheric coefficient vector,
                  into memory as VectorInterpolator objects."""

        # set up lookup table grid, and associated filename prefixes
        self.lut_dims, self.lut_grids, self.lut_names = [], [], []
        for key, val in self.lut_grid_config.items():
            self.lut_names.append(key)
            self.lut_grids.append(np.array(val))
            if len(self.lut_grids[-1]) == 1:
                raise ValueError('Only 1 value in LUT grid {}.  1-d LUT grids cannot be interpreted.'.format(key))
            self.lut_dims.append(len(val))
            if val != sorted(val):
                logging.error('Lookup table grid needs ascending order')
                raise ValueError('Lookup table grid needs ascending order')

        # "points" contains all combinations of grid points
        # We will have one filename prefix per point
        self.points = combos(self.lut_grids)
        self.files = []
        for point in self.points:
            outf = '_'.join(['%s-%6.4f' % (n, x)
                             for n, x in zip(self.lut_names, point)])
            self.files.append(outf)

        rebuild_cmds = []
        for point, fn in zip(self.points, self.files):
            try:
                cmd = self.rebuild_cmd(point, fn)
                rebuild_cmds.append(cmd)
            except FileExistsError:
                pass

        if self.configure_and_exit:
            raise SystemExit
            # sys.exit(0)

        elif len(rebuild_cmds) > 0 and self.auto_rebuild:
            logging.info("rebuilding")
            import multiprocessing
            cwd = os.getcwd()
            os.chdir(self.lut_dir)
            count = multiprocessing.cpu_count()
            pool = multiprocessing.Pool(processes=count)
            r = pool.map_async(spawn_rt, rebuild_cmds)
            r.wait()
            os.chdir(cwd)

    def summarize(self, x_RT, geom):
        """Summary of state vector."""

        if len(x_RT) < 1:
            return ''
        return 'Atmosphere: '+' '.join(['%s: %5.3f' % (si, xi) for si, xi in zip(self.statevec,x_RT[self._x_RT_index_for_point])])

