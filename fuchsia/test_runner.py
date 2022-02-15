#!/usr/bin/env python3
#
# Copyright 2018 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Deploys and runs a test package on a Fuchsia target."""

import argparse
import common
import os
import shutil
import sys
import tempfile

import ffx_session
from common_args import AddCommonArgs, AddTargetSpecificArgs, \
                        ConfigureLogging, GetDeploymentTargetForArgs
from net_test_server import SetupTestServer
from run_test_package import RunTestPackage, RunTestPackageArgs
from runner_exceptions import HandleExceptionAndReturnExitCode

DEFAULT_TEST_SERVER_CONCURRENCY = 4

TEST_DATA_DIR = '/tmp'
TEST_FILTER_PATH = TEST_DATA_DIR + '/test_filter.txt'
TEST_LLVM_PROFILE_DIR = 'llvm-profile'
TEST_PERF_RESULT_FILE = 'test_perf_summary.json'
TEST_RESULT_FILE = 'test_summary.json'

TEST_REALM_NAME = 'chromium_tests'

FILTER_DIR = 'testing/buildbot/filters'


class TestOutputs(object):
  """An abstract base class for extracting outputs generated by a test."""

  def __init__(self):
    pass

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    return False

  def GetFfxSession(self):
    raise NotImplementedError()

  def GetDevicePath(self, path):
    """Returns an absolute device-local variant of a path."""
    raise NotImplementedError()

  def GetFile(self, glob, destination):
    """Places all files/directories matched by a glob into a destination."""
    raise NotImplementedError()

  def GetCoverageProfiles(self, destination):
    """Places all coverage files from the target into a destination."""
    raise NotImplementedError()


class TargetTestOutputs(TestOutputs):
  """A TestOutputs implementation for CFv1 tests, where tests emit files into
  /tmp that are retrieved from the device via ssh."""

  def __init__(self, target, package_name, test_realms):
    super(TargetTestOutputs, self).__init__()
    self._target = target
    self._package_name = package_name
    self._test_realms = test_realms

  def GetFfxSession(self):
    return None  # ffx is not used to run CFv1 tests.

  def GetDevicePath(self, path):
    return TEST_DATA_DIR + '/' + path

  def GetFile(self, glob, destination):
    """Places all files/directories matched by a glob into a destination."""
    self._target.GetFile(self.GetDevicePath(glob),
                         destination,
                         for_package=self._package_name,
                         for_realms=self._test_realms)

  def GetCoverageProfiles(self, destination):
    # Copy all the files in the profile directory. /* is used instead of
    # recursively copying due to permission issues for the latter.
    self._target.GetFile(self.GetDevicePath(TEST_LLVM_PROFILE_DIR + '/*'),
                         destination, None, None)


class CustomArtifactsTestOutputs(TestOutputs):
  """A TestOutputs implementation for CFv2 tests, where tests emit files into
  /custom_artifacts that are retrieved from the device automatically via ffx."""

  def __init__(self, target):
    super(CustomArtifactsTestOutputs, self).__init__()
    self._target = target
    self._ffx_session_context = ffx_session.FfxSession(target._log_manager)
    self._ffx_session = None

  def __enter__(self):
    self._ffx_session = self._ffx_session_context.__enter__()
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    self._ffx_session = None
    self._ffx_session_context.__exit__(exc_type, exc_val, exc_tb)
    return False

  def GetFfxSession(self):
    assert self._ffx_session
    return self._ffx_session

  def GetDevicePath(self, path):
    return '/custom_artifacts/' + path

  def GetOutputDirectory(self):
    return self._ffx_session.get_output_dir()

  def GetFile(self, glob, destination):
    """Places all files/directories matched by a glob into a destination."""
    shutil.copy(
        os.path.join(self.GetOutputDirectory(), 'artifact-0', 'custom-0', glob),
        destination)

  def GetCoverageProfiles(self, destination):
    # Copy all the files in the profile directory.
    # TODO(https://fxbug.dev/77634): Switch to ffx-based extraction once it is
    # implemented.
    self._target.GetFile(
        '/tmp/test_manager:0/children/debug_data:0/data/' +
        TEST_LLVM_PROFILE_DIR + '/*', destination)


def MakeTestOutputs(component_version, target, package_name, test_realms):
  if component_version == '2':
    return CustomArtifactsTestOutputs(target)
  return TargetTestOutputs(target, package_name, test_realms)


def AddTestExecutionArgs(arg_parser):
  test_args = arg_parser.add_argument_group('testing',
                                            'Test execution arguments')
  test_args.add_argument('--gtest_filter',
                         help='GTest filter to use in place of any default.')
  test_args.add_argument(
      '--gtest_repeat',
      help='GTest repeat value to use. This also disables the '
      'test launcher timeout.')
  test_args.add_argument(
      '--test-launcher-retry-limit',
      help='Number of times that test suite will retry failing '
      'tests. This is multiplicative with --gtest_repeat.')
  test_args.add_argument('--test-launcher-print-test-stdio',
                         choices=['auto', 'always', 'never'],
                         help='Controls when full test output is printed.'
                         'auto means to print it when the test failed.')
  test_args.add_argument('--test-launcher-shard-index',
                         type=int,
                         default=os.environ.get('GTEST_SHARD_INDEX'),
                         help='Index of this instance amongst swarming shards.')
  test_args.add_argument('--test-launcher-total-shards',
                         type=int,
                         default=os.environ.get('GTEST_TOTAL_SHARDS'),
                         help='Total number of swarming shards of this suite.')
  test_args.add_argument('--gtest_break_on_failure',
                         action='store_true',
                         default=False,
                         help='Should GTest break on failure; useful with '
                         '--gtest_repeat.')
  test_args.add_argument('--single-process-tests',
                         action='store_true',
                         default=False,
                         help='Runs the tests and the launcher in the same '
                         'process. Useful for debugging.')
  test_args.add_argument('--test-launcher-batch-limit',
                         type=int,
                         help='Sets the limit of test batch to run in a single '
                         'process.')
  # --test-launcher-filter-file is specified relative to --out-dir,
  # so specifying type=os.path.* will break it.
  test_args.add_argument(
      '--test-launcher-filter-file',
      default=None,
      help='Filter file(s) passed to target test process. Use ";" to separate '
      'multiple filter files ')
  test_args.add_argument('--test-launcher-jobs',
                         type=int,
                         help='Sets the number of parallel test jobs.')
  test_args.add_argument('--test-launcher-summary-output',
                         help='Where the test launcher will output its json.')
  test_args.add_argument('--enable-test-server',
                         action='store_true',
                         default=False,
                         help='Enable Chrome test server spawner.')
  test_args.add_argument(
      '--test-launcher-bot-mode',
      action='store_true',
      default=False,
      help='Informs the TestLauncher to that it should enable '
      'special allowances for running on a test bot.')
  test_args.add_argument('--isolated-script-test-output',
                         help='If present, store test results on this path.')
  test_args.add_argument(
      '--isolated-script-test-perf-output',
      help='If present, store chartjson results on this path.')
  test_args.add_argument('--use-run',
                         dest='use_run_test_component',
                         default=True,
                         action='store_false',
                         help='Run the test package using run rather than '
                         'hermetically using run-test-component.')
  test_args.add_argument(
      '--code-coverage',
      default=False,
      action='store_true',
      help='Gather code coverage information and place it in '
      'the output directory.')
  test_args.add_argument('--code-coverage-dir',
                         default=os.getcwd(),
                         help='Directory to place code coverage information. '
                         'Only relevant when --code-coverage set to true. '
                         'Defaults to current directory.')
  test_args.add_argument('--child-arg',
                         action='append',
                         help='Arguments for the test process.')
  test_args.add_argument('--gtest_also_run_disabled_tests',
                         default=False,
                         action='store_true',
                         help='Run tests prefixed with DISABLED_')
  test_args.add_argument('child_args',
                         nargs='*',
                         help='Arguments for the test process.')


def MapFilterFileToPackageFile(filter_file):
  # TODO(crbug.com/1279803): Until one can send file to the device when running
  # a test, filter files must be read from the test package.
  if not FILTER_DIR in filter_file:
    raise ValueError('CFv2 tests only support registered filter files present '
                     ' in the test package')
  return '/pkg/' + filter_file[filter_file.index(FILTER_DIR):]


def MaybeApplyTestBotOverrides(parsed_args):
  """Overrides certain arguments when running on test bots."""

  if not parsed_args.test_launcher_bot_mode:
    return

  if common.GetHostArchFromPlatform() == 'arm64':
    # Cap the number of cores for ARM bots.
    # ARM-based test bots use container-level isolation, so the reported core
    # count from the system reflects the actual number of physical system cores,
    # not just the budget for this test task.
    parsed_args.cpu_cores = min(parsed_args.cpu_cores, 4)


def main():
  parser = argparse.ArgumentParser()
  AddTestExecutionArgs(parser)
  AddCommonArgs(parser)
  AddTargetSpecificArgs(parser)
  args = parser.parse_args()

  # Flag out_dir is required for tests launched with this script.
  if not args.out_dir:
    raise ValueError("out-dir must be specified.")

  if args.component_version == "2":
    args.use_run_test_component = False

  if (args.code_coverage and args.component_version != "2"
      and not args.use_run_test_component):
    if args.enable_test_server:
      # TODO(1254563): Tests that need access to the test server cannot be run
      # as test component under CFv1. Because code coverage requires it, force
      # the test to run as a test component. It is expected that test that tries
      # to use the external test server will fail.
      args.use_run_test_component = True
    else:
      raise ValueError('Collecting code coverage info requires using '
                       'run-test-component.')

  MaybeApplyTestBotOverrides(args)

  ConfigureLogging(args)

  child_args = []
  if args.test_launcher_shard_index != None:
    child_args.append(
        '--test-launcher-shard-index=%d' % args.test_launcher_shard_index)
  if args.test_launcher_total_shards != None:
    child_args.append(
        '--test-launcher-total-shards=%d' % args.test_launcher_total_shards)
  if args.single_process_tests:
    child_args.append('--single-process-tests')
  if args.test_launcher_bot_mode:
    child_args.append('--test-launcher-bot-mode')
  if args.test_launcher_batch_limit:
    child_args.append('--test-launcher-batch-limit=%d' %
                       args.test_launcher_batch_limit)

  # Only set --test-launcher-jobs if the caller specifies it, in general.
  # If the caller enables the test-server then we need to launch the right
  # number of instances to match the maximum number of parallel test jobs, so
  # in that case we set --test-launcher-jobs based on the number of CPU cores
  # specified for the emulator to use.
  test_concurrency = None
  if args.test_launcher_jobs:
    test_concurrency = args.test_launcher_jobs
  elif args.enable_test_server:
    if args.device == 'device':
      test_concurrency = DEFAULT_TEST_SERVER_CONCURRENCY
    else:
      test_concurrency = args.cpu_cores
  if test_concurrency:
    child_args.append('--test-launcher-jobs=%d' % test_concurrency)
  if args.test_launcher_print_test_stdio:
    child_args.append('--test-launcher-print-test-stdio=%s' %
                      args.test_launcher_print_test_stdio)

  if args.gtest_filter:
    child_args.append('--gtest_filter=' + args.gtest_filter)
  if args.gtest_repeat:
    child_args.append('--gtest_repeat=' + args.gtest_repeat)
    child_args.append('--test-launcher-timeout=-1')
  if args.test_launcher_retry_limit:
    child_args.append(
        '--test-launcher-retry-limit=' + args.test_launcher_retry_limit)
  if args.gtest_break_on_failure:
    child_args.append('--gtest_break_on_failure')
  if args.gtest_also_run_disabled_tests:
    child_args.append('--gtest_also_run_disabled_tests')

  if args.child_arg:
    child_args.extend(args.child_arg)
  if args.child_args:
    child_args.extend(args.child_args)

  test_realms = []
  if args.use_run_test_component:
    test_realms = [TEST_REALM_NAME]

  try:
    with GetDeploymentTargetForArgs(args) as target, \
         MakeTestOutputs(args.component_version,
                         target,
                         args.package_name,
                         test_realms) as test_outputs:
      if args.test_launcher_summary_output:
        child_args.append('--test-launcher-summary-output=' +
                          test_outputs.GetDevicePath(TEST_RESULT_FILE))
      if args.isolated_script_test_output:
        child_args.append('--isolated-script-test-output=' +
                          test_outputs.GetDevicePath(TEST_RESULT_FILE))
      if args.isolated_script_test_perf_output:
        child_args.append('--isolated-script-test-perf-output=' +
                          test_outputs.GetDevicePath(TEST_PERF_RESULT_FILE))

      target.Start()
      target.StartSystemLog(args.package)

      if args.test_launcher_filter_file:
        if args.component_version == "2":
          # TODO(crbug.com/1279803): Until one can send file to the device when
          # running a test, filter files must be read from the test package.
          test_launcher_filter_files = map(
              MapFilterFileToPackageFile,
              args.test_launcher_filter_file.split(';'))
          child_args.append('--test-launcher-filter-file=' +
                            ';'.join(test_launcher_filter_files))
        else:
          test_launcher_filter_files = args.test_launcher_filter_file.split(';')
          with tempfile.NamedTemporaryFile('a+b') as combined_filter_file:
            for filter_file in test_launcher_filter_files:
              with open(filter_file, 'rb') as f:
                combined_filter_file.write(f.read())
            combined_filter_file.seek(0)
            target.PutFile(combined_filter_file.name,
                           TEST_FILTER_PATH,
                           for_package=args.package_name,
                           for_realms=test_realms)
            child_args.append('--test-launcher-filter-file=' + TEST_FILTER_PATH)

      test_server = None
      if args.enable_test_server:
        assert test_concurrency
        test_server = SetupTestServer(target, test_concurrency,
                                      args.package_name, test_realms)

      run_package_args = RunTestPackageArgs.FromCommonArgs(args)
      if args.use_run_test_component:
        run_package_args.test_realm_label = TEST_REALM_NAME
        run_package_args.use_run_test_component = True
      if args.component_version == "2":
        run_package_args.output_directory = test_outputs.GetOutputDirectory()
      returncode = RunTestPackage(target, test_outputs.GetFfxSession(),
                                  args.package, args.package_name,
                                  args.component_version, child_args,
                                  run_package_args)

      if test_server:
        test_server.Stop()

      if args.code_coverage:
        test_outputs.GetCoverageProfiles(args.code_coverage_dir)

      if args.test_launcher_summary_output:
        test_outputs.GetFile(TEST_RESULT_FILE,
                             args.test_launcher_summary_output)

      if args.isolated_script_test_output:
        test_outputs.GetFile(TEST_RESULT_FILE, args.isolated_script_test_output)

      if args.isolated_script_test_perf_output:
        test_outputs.GetFile(TEST_PERF_RESULT_FILE,
                             args.isolated_script_test_perf_output)

      return returncode

  except:
    return HandleExceptionAndReturnExitCode()


if __name__ == '__main__':
  sys.exit(main())
