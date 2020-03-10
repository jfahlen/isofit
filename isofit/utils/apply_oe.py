#! /usr/bin/env python3
#
# Authors: David R Thompson and Philip G. Brodrick
#

import argparse
import os
import sys
from os.path import join, exists, split, abspath
from shutil import copyfile
from datetime import datetime
from spectral.io import envi
import logging
import json
import gdal
import numpy as np
from typing import List, Tuple

eps = 1e-6
chunksize = 256
segmentation_size = 400
num_integrations = 400

num_elev_lut_elements = 1
num_h2o_lut_elements = 5
num_to_sensor_azimuth_lut_elements = 1
num_to_sensor_zenith_lut_elements = 1

num_aerosol_1_lut_elements = 4
num_aerosol_2_lut_elements = 4

aerosol_1_lut_range = [0.001, 0.5]
aerosol_2_lut_range = [0.001, 0.5]

h2o_min = 0.2

uncorrelated_radiometric_uncertainty = 0.02

inversion_windows = [[400.0, 1300.0], [1450, 1780.0], [2050.0, 2450.0]]


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description="Representative subset")
    parser.add_argument('input_radiance', type=str)
    parser.add_argument('input_loc', type=str)
    parser.add_argument('input_obs', type=str)
    parser.add_argument('working_directory', type=str)
    parser.add_argument('sensor', type=str, choices=['ang', 'avcl'])
    parser.add_argument('--copy_input_files', type=int, choices=[0,1], default=0)
    parser.add_argument('--h2o', action='store_true')
    parser.add_argument('--isofit_path', type=str)
    parser.add_argument('--modtran_path', type=str)
    parser.add_argument('--wavelength_path', type=str)
    parser.add_argument('--aerosol_climatology_path', type=str, default=None)
    parser.add_argument('--rdn_factors_path', type=str)
    parser.add_argument('--surface_path', type=str)
    parser.add_argument('--channelized_uncertainty_path', type=str)
    parser.add_argument('--level', type=str, default="INFO")
    parser.add_argument('--nodata_value', type=float, default=-9999)
    parser.add_argument('--log_file', type=str, default=None)

    args = parser.parse_args()

    if args.copy_input_files == 1:
        args.copy_input_files = True
    else:
        args.copy_input_files = False

    if args.log_file is None:
        logging.basicConfig(format='%(message)s', level=args.level)
    else:
        logging.basicConfig(format='%(message)s', level=args.level, filename=args.log_file)


    paths = Pathnames(args)
    paths.make_directories()
    paths.stage_files()


    # TODO: Update this to a normal import, referencing the (likely ported) version of ISOFIT used for EMIT.
    sys.path.append(paths.isofit_path)
    from isofit.utils import segment, extractions, empirical_line
    from isofit.core import isofit


    # Based on the sensor type, get appropriate year/month/day info fro intial condition.
    # We'll adjust for line length and UTC day overrun later
    if args.sensor == 'ang':
        # parse flightline ID (AVIRIS-NG assumptions)
        dt = datetime.strptime(paths.fid[3:], '%Y%m%dt%H%M%S')
        dayofyear = dt.timetuple().tm_yday
    elif args.sensor == 'avcl':
        # parse flightline ID (AVIRIS-CL assumptions)
        dt = datetime.strptime('20{}t000000'.format(paths.fid[1:7]), '%Y%m%dt%H%M%S')
        dayofyear = dt.timetuple().tm_yday


    h_m_s, day_increment, mean_path_km, mean_to_sensor_azimuth, mean_to_sensor_zenith_rad, valid, \
    to_sensor_azimuth_lut_grid, to_sensor_zenith_lut_grid = get_metadata_from_obs(paths.obs_working_path)

    if day_increment:
        dayofyear += 1

    gmtime = float(h_m_s[0] + h_m_s[1] / 60.)

    # Superpixel segmentation
    if not exists(paths.lbl_working_path) or not exists(paths.radiance_working_path):
        logging.info('Segmenting...')
        segment(spectra=(paths.radiance_working_path, paths.lbl_working_path),
                flag=args.nodata_value, npca=5, segsize=segmentation_size, nchunk=chunksize)

    # Extract input data per segment
    for inp, outp in [(paths.radiance_working_path, paths.rdn_subs_path),
                      (paths.obs_working_path, paths.obs_subs_path),
                      (paths.loc_working_path, paths.loc_subs_path)]:
        if not exists(outp):
            logging.info('Extracting ' + outp)
            extractions(inputfile=inp, labels=paths.lbl_working_path,
                        output=outp, chunksize=chunksize, flag=args.nodata_value)

    # get radiance file, wavelengths
    if args.wavelength_path:
        chn, wl, fwhm = np.loadtxt(args.wavelength_path).T
    else:
        radiance_dataset = envi.open(paths.rdn_subs_path + '.hdr')
        wl = np.array([float(w) for w in radiance_dataset.metadata['wavelength']])
        if 'fwhm' in radiance_dataset.metadata:
            fwhm = np.array([float(f) for f in radiance_dataset.metadata['fwhm']])
        else:
            fwhm = np.ones(wl.shape) * (wl[1] - wl[0])

    # Convert to microns if needed
    if wl[0] > 100:
        wl = wl / 1000.0
        fwhm = fwhm / 1000.0

    # write wavelength file
    wl_data = np.concatenate([np.arange(len(wl))[:, np.newaxis], wl[:, np.newaxis],
                              fwhm[:, np.newaxis]], axis=1)
    np.savetxt(paths.wavelength_path, wl_data, delimiter=' ')

    mean_latitude, mean_longitude, mean_elevation_km, elevation_lut_grid = get_metadata_from_loc(paths.loc_working_path)

    mean_altitude_km = mean_elevation_km + np.cos(mean_to_sensor_zenith_rad) * mean_path_km

    logging.info('Path (km): %f, To-sensor Zenith (rad): %f, Mean Altitude: %6.2f km' %
                 (mean_path_km, mean_to_sensor_zenith_rad, mean_altitude_km))


    if not exists(paths.h2o_subs_path + '.hdr') or not exists(paths.h2o_subs_path):

        write_modtran_template(atmosphere_type='ATM_MIDLAT_SUMMER', fid=paths.fid, altitude_km=mean_altitude_km,
                               dayofyear=dayofyear, latitude=mean_latitude, longitude=mean_longitude,
                               to_sensor_azimuth=mean_to_sensor_azimuth, gmtime=gmtime, elevation_km=mean_elevation_km,
                               output_file=paths.h2o_template_path)


        ################# HERE ###############
        logging.info('Writing H2O pre-solve configuration file.')
        build_presolve_config(paths, num_integrations, uncorrelated_radiometric_uncertainty, inversion_windows,
                              (0.5, 5), 10)

        # Run modtran retrieval
        logging.info('Run ISOFIT initial guess')
        retrieval_h2o = isofit.Isofit(paths.h2o_config_path, level='DEBUG')
        retrieval_h2o.run()

        # clean up unneeded storage
        for to_rm in ['*r_k', '*t_k', '*tp7', '*wrn', '*psc', '*plt', '*7sc', '*acd']:
            cmd = 'rm ' + join(paths.lut_h2o_directory, to_rm)
            logging.info(cmd)
            os.system(cmd)

    # Extract h2o grid avoiding the zero label (periphery, bad data)
    # and outliers
    h2o = envi.open(paths.h2o_subs_path + '.hdr')
    h2o_est = h2o.read_band(-1)[:].flatten()

    h2o_lut_grid = np.linspace(np.percentile(
        h2o_est[h2o_est > h2o_min], 5), np.percentile(h2o_est[h2o_est > h2o_min], 95), num_h2o_lut_elements)

    #TODO: update to also include aerosols
    logging.info('Full (non-aerosol) LUTs:\nElevation: {}\nTo-sensor azimuth: {}\nTo-sensor zenith: {}\nh2o-vis: {}:'.format(elevation_lut_grid, to_sensor_azimuth_lut_grid, to_sensor_zenith_lut_grid, h2o_lut_grid))

    logging.info(paths.state_subs_path)
    if not exists(paths.state_subs_path) or \
            not exists(paths.uncert_subs_path) or \
            not exists(paths.rfl_subs_path):

        # TODO: consider only doing one modtran template write, it's a bit redundant
        write_modtran_template(atmosphere_type='ATM_MIDLAT_SUMMER', fid=paths.fid, altitude_km=mean_altitude_km,
                               dayofyear=dayofyear, latitude=mean_latitude, longitude=mean_longitude,
                               to_sensor_azimuth=mean_to_sensor_azimuth, gmtime=gmtime, elevation_km=mean_elevation_km,
                               output_file=paths.modtran_template_path)

        logging.info('Writing main configuration file.')
        build_main_config(paths, h2o_lut_grid, elevation_lut_grid, to_sensor_azimuth_lut_grid,
                          to_sensor_zenith_lut_grid, mean_latitude, mean_longitude, dt)

        # Run modtran retrieval
        logging.info('Running ISOFIT with full LUT')
        retrieval_full = isofit.Isofit(paths.modtran_config_path, level='DEBUG')
        retrieval_full.run()

        # clean up unneeded storage
        for to_rm in ['*r_k', '*t_k', '*tp7', '*wrn', '*psc', '*plt', '*7sc', '*acd']:
            cmd = 'rm ' + join(paths.lut_modtran_directory, to_rm)
            logging.info(cmd)
            os.system(cmd)

    if not exists(paths.rfl_working_path) or not exists(paths.uncert_working_path):
        # Empirical line
        logging.info('Empirical line inference')
        empirical_line(reference_radiance=paths.rdn_subs_path,
                       reference_reflectance=paths.rfl_subs_path,
                       reference_uncertainty=paths.uncert_subs_path,
                       reference_locations=paths.loc_subs_path,
                       hashfile=paths.lbl_working_path,
                       input_radiance=paths.radiance_working_path,
                       input_locations=paths.loc_working_path,
                       output_reflectance=paths.rfl_working_path,
                       output_uncertainty=paths.uncert_working_path,
                       isofit_config=paths.modtran_config_path)

    logging.info('Done.')

class Pathnames():

    def __init__(self, args):

        # Determine FID based on sensor name
        if args.sensor == 'ang':
            self.fid = split(args.input_radiance)[-1][:18]
            logging.info('Flightline ID: %s' % self.fid)
        elif args.sensor == 'avcl':
            self.fid = split(args.input_radiance)[-1][:16]
            logging.info('Flightline ID: %s' % self.fid)

        # Names from inputs
        self.aerosol_climatology = args.aerosol_climatology_path
        self.input_radiance_file = args.input_radiance
        self.input_loc_file = args.input_loc
        self.input_obs_file = args.input_obs
        self.working_directory = abspath(args.working_directory)

        self.lut_modtran_directory = abspath(join(self.working_directory, 'lut_full/'))

        if args.surface_path:
            self.surface_path = args.surface_path
        else:
            self.surface_path = os.getenv('ISOFIT_SURFACE_MODEL')

        # set up some sub-directories
        self.lut_h2o_directory = abspath(join(self.working_directory, 'lut_h2o/'))
        self.config_directory = abspath(join(self.working_directory, 'config/'))
        self.data_directory = abspath(join(self.working_directory, 'data/'))
        self.input_data_directory = abspath(join(self.working_directory, 'input/'))
        self.output_directory = abspath(join(self.working_directory, 'output/'))


        # define all output names
        rdn_fname = self.fid + '_rdn'
        self.rfl_working_path = abspath(join(self.output_directory, rdn_fname.replace('_rdn', '_rfl')))
        self.uncert_working_path = abspath(join(self.output_directory, rdn_fname.replace('_rdn', '_uncert')))
        self.lbl_working_path = abspath(join(self.output_directory, rdn_fname.replace('_rdn', '_lbl')))
        self.surface_working_path = abspath(join(self.data_directory, 'surface.mat'))

        if args.copy_input_files is True:
            self.radiance_working_path = abspath(join(self.input_data_directory, rdn_fname))
            self.obs_working_path = abspath(join(self.input_data_directory, self.fid + '_obs'))
            self.loc_working_path = abspath(join(self.input_data_directory, self.fid + '_loc'))
        else:
            self.radiance_working_path = self.input_radiance_file
            self.obs_working_path = self.input_obs_file
            self.loc_working_path = self.input_loc_file

        if args.channelized_uncertainty_path:
            self.input_channelized_uncertainty_path = args.channelized_uncertainty_path
        else:
            self.input_channelized_uncertainty_path = os.getenv('ISOFIT_CHANNELIZED_UNCERTAINTY')

        self.channelized_uncertainty_working_path = abspath(join(self.data_directory, 'channelized_uncertainty.txt'))

        self.rdn_subs_path = abspath(join(self.input_data_directory, self.fid + '_subs_rdn'))
        self.obs_subs_path = abspath(join(self.input_data_directory, self.fid + '_subs_obs'))
        self.loc_subs_path = abspath(join(self.input_data_directory, self.fid + '_subs_loc'))
        self.rfl_subs_path = abspath(join(self.output_directory, self.fid + '_subs_rfl'))
        self.state_subs_path = abspath(join(self.output_directory, self.fid + '_subs_state'))
        self.uncert_subs_path = abspath(join(self.output_directory, self.fid + '_subs_uncert'))
        self.h2o_subs_path = abspath(join(self.output_directory, self.fid + '_subs_h2o'))

        self.wavelength_path = abspath(join(self.data_directory, 'wavelengths.txt'))

        self.modtran_template_path = abspath(join(self.config_directory, self.fid + '_modtran_tpl.json'))
        self.h2o_template_path = abspath(join(self.config_directory, self.fid + '_h2o_tpl.json'))

        self.modtran_config_path = abspath(join(self.config_directory, self.fid + '_modtran.json'))
        self.h2o_config_path = abspath(join(self.config_directory, self.fid + '_h2o.json'))

        if args.modtran_path:
            self.modtran_path = args.modtran_path
        else:
            self.modtran_path = os.getenv('MODTRAN_DIR')

        if args.isofit_path:
            self.isofit_path = args.isofit_path
        else:
            self.isofit_path = os.getenv('ISOFIT_BASE')

        if args.sensor == 'ang':
            self.noise_path = join(self.isofit_path, 'data', 'avirisng_noise.txt')
        elif args.sensor == 'avcl':
            self.noise_path = join(self.isofit_path, 'data', 'avirisc_noise.txt')
        else:
            logging.info('no noise path found, check sensor type')
            quit()

        self.aerosol_tpl_path = join(self.isofit_path, 'data', 'aerosol_template.json')
        self.rdn_factors_path = args.rdn_factors_path

    def make_directories(self):
        # create missing directories
        for dpath in [self.working_directory, self.lut_h2o_directory, self.lut_modtran_directory, self.config_directory,
                      self.data_directory, self.input_data_directory, self.output_directory]:
            if not exists(dpath):
                os.mkdir(dpath)

    def stage_files(self):
        # stage data files by copying into working directory
        files_to_stage = [(self.input_radiance_file, self.radiance_working_path, True),
                          (self.input_obs_file, self.obs_working_path, True),
                          (self.input_loc_file, self.loc_working_path, True),
                          (self.surface_path, self.surface_working_path, False)]

        if (self.input_channelized_uncertainty_path is not None):
            files_to_stage.append((self.input_channelized_uncertainty_path, self.channelized_uncertainty_working_path, False))
        else:
            self.channelized_uncertainty_working_path = None
            logging.info('No valid channelized uncertainty file found, proceeding without uncertainty')


        for src, dst, hasheader in files_to_stage:
            if not exists(dst):
                logging.info('Staging %s to %s' % (src, dst))
                copyfile(src, dst)
                if hasheader:
                    copyfile(src + '.hdr', dst + '.hdr')


class SerialEncoder(json.JSONEncoder):
    """Encoder for json to help ensure json objects can be passed to the workflow manager.

    """

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        else:
            return super(SerialEncoder, self).default(obj)


def load_climatology(config_path: str, latitude: float, longitude: float, acquisition_datetime: datetime, isofit_path: str):
    """ Load climatology data, based on location and configuration
    Args:
        config_path: path to the base configuration directory for isofit
        latitude: latitude to set for the segment (mean of acquisition suggested)
        longitude: latitude to set for the segment (mean of acquisition suggested)
        acquisition_datetime: datetime to use for the segment( mean of acquisition suggested)
        isofit_path: base path to isofit installation (needed for data path references)

    :Returns
        aerosol_state_vector: A dictionary that defines the aerosol state vectors for isofit
        aerosol_lut_grid: A dictionary of the aerosol lookup table (lut) grid to be explored
        aerosol_model_path: A path to the location of the aerosol model to use with MODTRAN.
    """

    aerosol_model_path = join(isofit_path, 'data', 'aerosol_twopart_model.txt')
    aerosol_1_lut = np.linspace(aerosol_1_lut_range[0], aerosol_1_lut_range[1], num_aerosol_1_lut_elements)
    aerosol_2_lut = np.linspace(aerosol_2_lut_range[0], aerosol_2_lut_range[1], num_aerosol_2_lut_elements)
    aerosol_lut_grid = {"AERFRAC_0": [float(q) for q in aerosol_1_lut],
                        "AERFRAC_1": [float(q) for q in aerosol_2_lut]}
    aerosol_state_vector = {
        "AERFRAC_0": {
            "bounds": [float(aerosol_1_lut_range[0]), float(aerosol_1_lut_range[1])],
            "scale": 1,
            "init": float((aerosol_1_lut_range[1]-aerosol_1_lut_range[0])/10. + aerosol_1_lut_range[0]),
            "prior_sigma": 10.0,
            "prior_mean": float((aerosol_1_lut_range[1]-aerosol_1_lut_range[0])/10. + aerosol_1_lut_range[0])},
        "AERFRAC_1": {
            "bounds": [float(aerosol_2_lut_range[0]), float(aerosol_2_lut_range[1])],
            "scale": 1,
            "init": float((aerosol_2_lut_range[1]-aerosol_2_lut_range[0])/2. + aerosol_2_lut_range[0]),
            "prior_sigma": 10.0,
            "prior_mean": float((aerosol_2_lut_range[1]-aerosol_2_lut_range[0])/2. + aerosol_2_lut_range[0])}}

    logging.info('Loading Climatology')
    # If a configuration path has been provided, use it to get relevant info
    if config_path is not None:
        month = acquisition_datetime.timetuple().tm_mon
        year = acquisition_datetime.timetuple().tm_year
        with open(config_path, 'r') as fin:
            for case in json.load(fin)['cases']:
                match = True
                logging.info('matching', latitude, longitude, month, year)
                for criterion, interval in case['criteria'].items():
                    logging.info(criterion, interval, '...')
                    if criterion == 'latitude':
                        if latitude < interval[0] or latitude > interval[1]:
                            match = False
                    if criterion == 'longitude':
                        if longitude < interval[0] or longitude > interval[1]:
                            match = False
                    if criterion == 'month':
                        if month < interval[0] or month > interval[1]:
                            match = False
                    if criterion == 'year':
                        if year < interval[0] or year > interval[1]:
                            match = False

                if match:
                    aerosol_state_vector = case['aerosol_state_vector']
                    aerosol_lut_grid = case['aerosol_lut_grid']
                    aerosol_model_path = case['aerosol_mdl_path']
                    break

    logging.info('Climatology Loaded.  Aerosol State Vector:\n{}\nAerosol LUT Grid:\n{}\nAerosol model path:{}'.format(
        aerosol_state_vector, aerosol_lut_grid, aerosol_model_path))
    return aerosol_state_vector, aerosol_lut_grid, aerosol_model_path


def get_time_from_obs(obs_filename: str, time_band: int = 9, max_flight_duration_h: int = 8):
    """ Scan through the obs file and find mean flight time
    Args:
        obs_filename: observation file name
        time_band: time band inside of observation file (normally 9)
        max_flight_duration_h: assumed maximum length of a flight

    :Returns:
        h_m_s: list of the hour, minute, and second mean of the given data section
        increment_day: a boolean to indicate if the mean day is greater than the starting day
    """
    dataset = gdal.Open(obs_filename, gdal.GA_ReadOnly)
    min_time = 25
    max_time = -1
    mean_time = np.zeros(dataset.RasterYSize)
    mean_time_w = np.zeros(dataset.RasterYSize)
    for line in range(dataset.RasterYSize):
        local_time = dataset.ReadAsArray(0, line, dataset.RasterXSize, 1)[time_band, ...]
        local_time = local_time[local_time != -9999]
        min_time = min(min_time, np.min(local_time))
        max_time = max(max_time, np.max(local_time))
        mean_time[line] = np.mean(local_time)
        mean_time_w[line] = np.prod(local_time.shape)

    mean_time = np.average(mean_time, weights=mean_time_w)

    increment_day = False
    # UTC day crossover corner case
    if (max_time > 24 - max_flight_duration_h and
            min_time < max_flight_duration_h):
        mean_time[mean_time < max_flight_duration_h] += 24
        mean_time = np.average(mean_time, weights=mean_time_w)

        # This means the majority of the line was really in the next UTC day,
        # increment the line accordingly
        if (mean_time > 24):
            mean_time -= 24
            increment_day = True

    # Calculate hour, minute, second
    h_m_s = [np.floor(mean_time)]
    h_m_s.append(np.floor((mean_time - h_m_s[-1]) * 60))
    h_m_s.append(np.floor((mean_time - h_m_s[-2] - h_m_s[-1] / 60.) * 3600))

    return h_m_s, increment_day


def get_metadata_from_obs(obs_file: str, trim_lines: int = 5,
                          max_flight_duration_h: int = 8, nodata_value = -9999):

    obs_dataset = gdal.Open(obs_file, gdal.GA_ReadOnly)

    # Initialize values to populate
    valid = np.zeros((obs_dataset.RasterYSize, obs_dataset.RasterXSize), dtype=bool)

    path_km = np.zeros((obs_dataset.RasterYSize, obs_dataset.RasterXSize))
    to_sensor_azimuth = np.zeros((obs_dataset.RasterYSize, obs_dataset.RasterXSize))
    to_sensor_zenith = np.zeros((obs_dataset.RasterYSize, obs_dataset.RasterXSize))
    time = np.zeros((obs_dataset.RasterYSize, obs_dataset.RasterXSize))

    for line in range(obs_dataset.RasterYSize):

        # Read line in
        obs_line = obs_dataset.ReadAsArray(0, line, obs_dataset.RasterXSize, 1)

        # Populate valid
        valid[line,:] = np.logical_not(np.any(obs_line == nodata_value,axis=0))

        path_km[line,:] = obs_line[0, ...] / 1000.
        to_sensor_azimuth[line,:] = obs_line[1, ...]
        to_sensor_zenith[line,:] = 180. - obs_line[2, ...]
        time[line,:] = obs_line[9, ...]

    if trim_lines != 0:
        actual_valid = valid.copy()
        valid[:trim_lines,:] = False
        valid[-trim_lines:,:] = False

    mean_path_km = np.mean(path_km[valid])
    del path_km

    mean_to_sensor_azimuth = np.mean(to_sensor_azimuth[valid])
    mean_to_sensor_zenith_rad = (np.mean(180 - to_sensor_zenith[valid]) / 360.0 * 2.0 * np.pi)

    geom_margin = eps * 2.0
    if (num_to_sensor_zenith_lut_elements == 1):
        to_sensor_zenith_lut_grid = None
    else:
        to_sensor_zenith_lut_grid = np.linspace(max((to_sensor_zenith[valid].min() - geom_margin), 0),
                                                180.0,num_to_sensor_zenith_lut_elements)

    if (num_to_sensor_azimuth_lut_elements == 1):
        to_sensor_azimuth_lut_grid = None
    else:
        # TODO: check mod logic
        to_sensor_azimuth_lut_grid = np.linspace((to_sensor_azimuth[valid].min() - geom_margin) % 360,
                                                 (to_sensor_azimuth[valid].max() + geom_margin) % 360,
                                                 num_to_sensor_azimuth_lut_elements)
    del to_sensor_azimuth
    del to_sensor_zenith

    # Make time calculations
    mean_time = np.mean(time[valid])
    min_time = np.min(time[valid])
    max_time = np.max(time[valid])

    increment_day = False
    # UTC day crossover corner case
    if (max_time > 24 - max_flight_duration_h and
            min_time < max_flight_duration_h):
        time[np.logical_and(time < max_flight_duration_h,valid)] += 24
        mean_time = np.mean(time[valid])

        # This means the majority of the line was really in the next UTC day,
        # increment the line accordingly
        if (mean_time > 24):
            mean_time -= 24
            increment_day = True

    # Calculate hour, minute, second
    h_m_s = [np.floor(mean_time)]
    h_m_s.append(np.floor((mean_time - h_m_s[-1]) * 60))
    h_m_s.append(np.floor((mean_time - h_m_s[-2] - h_m_s[-1] / 60.) * 3600))

    if trim_lines != 0:
        valid = actual_valid

    return h_m_s, increment_day, mean_path_km, mean_to_sensor_azimuth, mean_to_sensor_zenith_rad, valid, \
           to_sensor_azimuth_lut_grid, to_sensor_zenith_lut_grid


def get_metadata_from_loc(loc_file: str, trim_lines: int = 5,
                          nodata_value=-9999):

    loc_dataset = gdal.Open(loc_file, gdal.GA_ReadOnly)

    loc_data = np.zeros((loc_dataset.RasterCount, loc_dataset.RasterYSize, loc_dataset.RasterXSize))
    for line in range(loc_dataset.RasterYSize):
        # Read line in
        loc_data[:,line:line+1,:] = loc_dataset.ReadAsArray(0, line, loc_dataset.RasterXSize, 1)

    valid = np.logical_not(np.any(loc_data == nodata_value,axis=0))
    if trim_lines != 0:
        valid[:trim_lines, :] = False
        valid[-trim_lines:, :] = False

    # Grab zensor position and orientation information
    mean_latitude = np.mean(loc_data[1,valid])
    mean_longitude = -np.mean(loc_data[0,valid])

    mean_elevation_km = np.mean(loc_data[2,valid]) / 1000.0

    # make elevation grid
    if num_elev_lut_elements == 1:
        elevation_lut_grid = None
    else:
        min_elev = np.min(loc_data[2, valid])/1000.
        max_elev = np.max(loc_data[2, valid])/1000.
        elevation_lut_grid = np.linspace(max(min_elev, eps),
                                         max_elev,
                                         num_elev_lut_elements)

    return mean_latitude, mean_longitude, mean_elevation_km, elevation_lut_grid



def build_presolve_config(paths: Pathnames, num_integrations: int, uncorrelated_radiometric_uncertainty: float,
                          inversion_windows: List, h2o_lut_range: Tuple, num_h2o_lut_elements: int):

    h2o_grid = np.linspace(h2o_lut_range[0], h2o_lut_range[1], num_h2o_lut_elements)

    h2o_configuration = {
        "wavelength_file": paths.wavelength_path,
        "lut_path": paths.lut_h2o_directory,
        "modtran_template_file": paths.h2o_template_path,
        "modtran_directory": paths.modtran_path,
        "statevector": {
            "H2OSTR": {
                "bounds": [float(np.min(h2o_grid)), float(np.max(h2o_grid))],
                "scale": 0.01,
                "init": np.percentile(h2o_grid,25),
                "prior_sigma": 100.0,
                "prior_mean": 1.5}
        },
        "lut_grid": {
            "H2OSTR": [float(x) for x in h2o_grid],
        },
        "unknowns": {
            "H2O_ABSCO": 0.0
        },
        "domain": {"start": 340, "end": 2520, "step": 0.1}
    }

    # make isofit configuration
    isofit_config_h2o = {'ISOFIT_base': paths.isofit_path,
                         'input': {'measured_radiance_file': paths.rdn_subs_path,
                                   'loc_file': paths.loc_subs_path,
                                   'obs_file': paths.obs_subs_path},
                         'output': {'estimated_state_file': paths.h2o_subs_path},
                         'forward_model': {
                             'instrument': {'wavelength_file': paths.wavelength_path,
                                            'parametric_noise_file': paths.noise_path,
                                            'integrations': num_integrations,
                                            'unknowns': {
                                                'uncorrelated_radiometric_uncertainty': uncorrelated_radiometric_uncertainty}},
                                                    'multicomponent_surface': {'wavelength_file': paths.wavelength_path,
                                                                               'surface_file': paths.surface_working_path,
                                                                               'select_on_init': True},
                             'modtran_radiative_transfer': h2o_configuration},
                         'inversion': {'windows': inversion_windows}}

    if paths.channelized_uncertainty_working_path is not None:
        isofit_config_h2o['forward_model']['unknowns'][
            'channelized_radiometric_uncertainty_file'] = paths.channelized_uncertainty_working_path

    if paths.rdn_factors_path:
        isofit_config_h2o['input']['radiometry_correction_file'] = paths.rdn_factors_path

    # write modtran_template
    with open(paths.h2o_config_path, 'w') as fout:
        fout.write(json.dumps(isofit_config_h2o, cls=SerialEncoder, indent=4, sort_keys=True))


def build_main_config(paths, h2o_lut_grid: np.array, elevation_lut_grid: np.array,
                      to_sensor_azimuth_lut_grid: np.array, to_sensor_zenith_lut_grid: np.array, mean_latitude, mean_longitude, dt):

    modtran_configuration = {
        "wavelength_file": paths.wavelength_path,
        "lut_path": paths.lut_modtran_directory,
        "aerosol_template_file": paths.aerosol_tpl_path,
        "modtran_template_file": paths.modtran_template_path,
        "modtran_directory": paths.modtran_path,
        "statevector": {
            "H2OSTR": {
                "bounds": [h2o_lut_grid[0], h2o_lut_grid[-1]],
                "scale": 0.01,
                "init": (h2o_lut_grid[1] + h2o_lut_grid[-1]) / 2.0,
                "prior_sigma": 100.0,
                "prior_mean": (h2o_lut_grid[1] + h2o_lut_grid[-1]) / 2.0,
            }
        },
        "lut_grid": {},
        "unknowns": {
            "H2O_ABSCO": 0.0
        },
        "domain": {"start": 340, "end": 2520, "step": 0.1}
    }
    if h2o_lut_grid is not None:
        modtran_configuration['lut_grid']['H2OSTR'] = [max(0.0, float(q)) for q in h2o_lut_grid]
    if elevation_lut_grid is not None:
        modtran_configuration['lut_grid']['GNDALT'] =  [max(0.0, float(q)) for q in elevation_lut_grid]
    if to_sensor_azimuth_lut_grid is not None:
        modtran_configuration['lut_grid']['TRUEAZ'] = [float(q) for q in to_sensor_azimuth_lut_grid]
    if to_sensor_zenith_lut_grid is not None:
        modtran_configuration['lut_grid']['OBSZEN'] = [float(q) for q in to_sensor_zenith_lut_grid]

    # add aerosol elements from climatology
    aerosol_state_vector, aerosol_lut_grid, aerosol_model_path = \
        load_climatology(paths.aerosol_climatology, mean_latitude, mean_longitude, dt,
                         paths.isofit_path)
    modtran_configuration['statevector'].update(aerosol_state_vector)
    modtran_configuration['lut_grid'].update(aerosol_lut_grid)
    modtran_configuration['aerosol_model_file'] = aerosol_model_path

    # make isofit configuration
    isofit_config_modtran = {'ISOFIT_base': paths.isofit_path,
                             'input': {'measured_radiance_file': paths.rdn_subs_path,
                                       'loc_file': paths.loc_subs_path,
                                       'obs_file': paths.obs_subs_path},
                             'output': {'estimated_state_file': paths.state_subs_path,
                                        'posterior_uncertainty_file': paths.uncert_subs_path,
                                        'estimated_reflectance_file': paths.rfl_subs_path},
                             'forward_model': {
                                 'instrument': {'wavelength_file': paths.wavelength_path,
                                                'parametric_noise_file': paths.noise_path,
                                                'integrations': num_integrations,
                                                'unknowns': {
                                                    'uncorrelated_radiometric_uncertainty': uncorrelated_radiometric_uncertainty}},
                                 "multicomponent_surface": {"wavelength_file": paths.wavelength_path,
                                                            "surface_file": paths.surface_working_path,
                                                            "select_on_init": True},
                                 "modtran_radiative_transfer": modtran_configuration},
                             "inversion": {"windows": inversion_windows}}

    if paths.channelized_uncertainty_working_path is not None:
        isofit_config_modtran['forward_model']['unknowns'][
            'channelized_radiometric_uncertainty_file'] = paths.channelized_uncertainty_working_path

    if paths.rdn_factors_path:
        isofit_config_modtran['input']['radiometry_correction_file'] = \
            paths.rdn_factors_path

    # write modtran_template
    with open(paths.modtran_config_path, 'w') as fout:
        fout.write(json.dumps(isofit_config_modtran, cls=SerialEncoder, indent=4, sort_keys=True))


def write_modtran_template(atmosphere_type: str, fid: str, altitude_km: float, dayofyear: int,
                           latitude: float, longitude: float, to_sensor_azimuth: float, gmtime: float,
                           elevation_km: float, output_file: str):
    """ Write a MODTRAN template file for use by isofit look up tables
    Args:
        atmosphere_type: label for the type of atmospheric profile to use in modtran
        fid: flight line id (name)
        altitude_km: altitude of the sensor in km
        dayofyear: the current day of the given year
        latitude: acquisition latitude
        longitude: acquisition longitude
        to_sensor_azimuth: azimuth view angle to the sensor, in degrees TODO - verify that this is/should be in degrees
        gmtime: greenwich mean time
        elevation_km: elevation of the land surface in km
        output_file: location to write the modtran template file to

    :Returns:
        None
    """
    # make modtran configuration
    h2o_template = {"MODTRAN": [{
        "MODTRANINPUT": {
            "NAME": fid,
            "DESCRIPTION": "",
            "CASE": 0,
            "RTOPTIONS": {
                "MODTRN": "RT_CORRK_FAST",
                "LYMOLC": False,
                "T_BEST": False,
                "IEMSCT": "RT_SOLAR_AND_THERMAL",
                "IMULT": "RT_DISORT",
                "DISALB": False,
                "NSTR": 8,
                "SOLCON": 0.0
            },
            "ATMOSPHERE": {
                "MODEL": atmosphere_type,
                "M1": atmosphere_type,
                "M2": atmosphere_type,
                "M3": atmosphere_type,
                "M4": atmosphere_type,
                "M5": atmosphere_type,
                "M6": atmosphere_type,
                "CO2MX": 410.0,
                "H2OSTR": 1.0,
                "H2OUNIT": "g",
                "O3STR": 0.3,
                "O3UNIT": "a"
            },
            "AEROSOLS": {"IHAZE": "AER_NONE"},
            "GEOMETRY": {
                "ITYPE": 3,
                "H1ALT": altitude_km,
                "IDAY": dayofyear,
                "IPARM": 11,
                "PARM1": latitude,
                "PARM2": longitude,
                "TRUEAZ": to_sensor_azimuth,
                "GMTIME": gmtime
            },
            "SURFACE": {
                "SURFTYPE": "REFL_LAMBER_MODEL",
                "GNDALT": elevation_km,
                "NSURF": 1,
                "SURFP": {"CSALB": "LAMB_CONST_0_PCT"}
            },
            "SPECTRAL": {
                "V1": 340.0,
                "V2": 2520.0,
                "DV": 0.1,
                "FWHM": 0.1,
                "YFLAG": "R",
                "XFLAG": "N",
                "FLAGS": "NT A   ",
                "BMNAME": "p1_2013"
            },
            "FILEOPTIONS": {
                "NOPRNT": 2,
                "CKPRNT": True
            }
        }
    }]}

    # write modtran_template
    with open(output_file, 'w') as fout:
        fout.write(json.dumps(h2o_template, cls=SerialEncoder, indent=4, sort_keys=True))



if __name__ == "__main__":
    main()