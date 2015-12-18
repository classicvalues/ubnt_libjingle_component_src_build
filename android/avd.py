#!/usr/bin/env python
# Copyright (c) 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Launches Android Virtual Devices with a set configuration for testing Chrome.

The script will launch a specified number of Android Virtual Devices (AVD's).
"""

import argparse
import logging
import os
import re
import sys

import install_emulator_deps

from devil.utils import cmd_helper
from pylib import constants
from pylib.utils import emulator

def main(argv):
  # ANDROID_SDK_ROOT needs to be set to the location of the SDK used to launch
  # the emulator to find the system images upon launch.
  emulator_sdk = constants.ANDROID_SDK_ROOT
  os.environ['ANDROID_SDK_ROOT'] = emulator_sdk

  arg_parser = argparse.ArgumentParser(description='AVD script.')
  sub_parsers = arg_parser.add_subparsers(title='subparser', dest='command')
  sub_parsers.add_parser(
      'kill', help='Shutdown all existing emulators')
  sub_parsers.add_parser(
      'delete', help='Deleting all the avd files')
  run_parser = sub_parsers.add_parser('run', help='Run emulators')
  run_parser.add_argument('--name', help='Optinaly, name of existing AVD to '
                          'launch. If not specified, AVD\'s will be created')
  run_parser.add_argument('-n', '--num', dest='emulator_count',
                          help='Number of emulators to launch (default is 1).',
                          type=int, default='1')
  run_parser.add_argument('--abi', default='x86',
                          help='Platform of emulators to launch (x86 default)')
  run_parser.add_argument('--api-level', dest='api_level',
                          help='API level for the image',
                          type=int, default=constants.ANDROID_SDK_VERSION)
  run_parser.add_argument('--sdcard-size', dest='sdcard_size',
                          default=emulator.DEFAULT_SDCARD_SIZE,
                          help='Set sdcard size of the emulators'
                          ' e.g. --sdcard-size=512M')
  run_parser.add_argument('--partition-size', dest='partition_size',
                          default=emulator.DEFAULT_STORAGE_SIZE,
                          help='Default internal storage size'
                          ' e.g. --partition-size=1024M')
  run_parser.add_argument('--launch-without-kill', action='store_false',
                          dest='kill_and_launch', default=True,
                          help='Kill all emulators at launch')

  arguments = arg_parser.parse_args(argv[1:])

  logging.root.setLevel(logging.INFO)

  if arguments.command == 'kill':
    logging.info('Killing all existing emulator and existing the program')
    emulator.KillAllEmulators()
    return
  if arguments.command == 'delete':
    emulator.DeleteAllTempAVDs()
    return

  # Check if SDK exist in ANDROID_SDK_ROOT
  if not install_emulator_deps.CheckSDK():
    raise Exception('Emulator SDK not installed in %s'
                     % constants.ANDROID_SDK_ROOT)

  # Check if KVM is enabled for x86 AVD's and check for x86 system images.
  # TODO(andrewhayden) Since we can fix all of these with install_emulator_deps
  # why don't we just run it?
  if arguments.abi == 'x86':
    if not install_emulator_deps.CheckKVM():
      logging.critical('ERROR: KVM must be enabled in BIOS, and installed. '
                       'Enable KVM in BIOS and run install_emulator_deps.py')
      return 1
    elif not install_emulator_deps.CheckX86Image(arguments.api_level):
      logging.critical('ERROR: System image for x86 AVD not installed. Run '
                       'install_emulator_deps.py')
      return 1

  # If AVD is specified, check that the SDK has the required target. If not,
  # check that the SDK has the desired target for the temporary AVD's.
  api_level = arguments.api_level
  if arguments.name:
    android = os.path.join(constants.ANDROID_SDK_ROOT, 'tools',
                           'android')
    avds_output = cmd_helper.GetCmdOutput([android, 'list', 'avd'])
    names = re.findall(r'Name: (\w+)', avds_output)
    api_levels = re.findall(r'API level (\d+)', avds_output)
    try:
      avd_index = names.index(arguments.name)
    except ValueError:
      logging.critical('ERROR: Specified AVD %s does not exist.',
                       arguments.name)
      return 1
    api_level = int(api_levels[avd_index])

  if not install_emulator_deps.CheckSDKPlatform(api_level):
    logging.critical('ERROR: Emulator SDK missing required target for API %d. '
                     'Run install_emulator_deps.py.')
    return 1

  if arguments.name:
    emulator.LaunchEmulator(
        arguments.name,
        arguments.abi,
        kill_and_launch=arguments.reset_and_launch,
        sdcard_size=arguments.sdcard_size,
        storage_size=arguments.partition_size
    )
  else:
    emulator.LaunchTempEmulators(
        arguments.emulator_count,
        arguments.abi,
        arguments.api_level,
        kill_and_launch=arguments.kill_and_launch,
        sdcard_size=arguments.sdcard_size,
        storage_size=arguments.partition_size,
        wait_for_boot=True
    )

if __name__ == '__main__':
  sys.exit(main(sys.argv))
