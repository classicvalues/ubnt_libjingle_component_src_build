#!/usr/bin/env python
# encoding: utf-8
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Compile Android resources into an intermediate APK.

This can also generate an R.txt, and an .srcjar file containing the proper
final R.java class for all resource packages the APK depends on.

This will crunch images with aapt2.
"""

import argparse
import collections
import contextlib
import filecmp
import multiprocessing.pool
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from xml.etree import ElementTree

from util import build_utils
from util import diff_utils
from util import manifest_utils
from util import resource_utils

# Name of environment variable that can be used to force this script to
# put temporary resource files into specific sub-directories, instead of
# temporary ones.
_ENV_DEBUG_VARIABLE = 'ANDROID_DEBUG_TEMP_RESOURCES_DIR'

# Import jinja2 from third_party/jinja2
sys.path.insert(1, os.path.join(build_utils.DIR_SOURCE_ROOT, 'third_party'))
from jinja2 import Template # pylint: disable=F0401

# Pngs that we shouldn't convert to webp. Please add rationale when updating.
_PNG_WEBP_BLACKLIST_PATTERN = re.compile('|'.join([
    # Crashes on Galaxy S5 running L (https://crbug.com/807059).
    r'.*star_gray\.png',
    # Android requires pngs for 9-patch images.
    r'.*\.9\.png',
    # Daydream requires pngs for icon files.
    r'.*daydream_icon_.*\.png']))


def _ListToDictionary(lst, separator):
  """Splits each element of the passed-in |lst| using |separator| and creates
  dictionary treating first element of the split as the key and second as the
  value."""
  return dict(item.split(separator, 1) for item in lst)


def _ParseArgs(args):
  """Parses command line options.

  Returns:
    An options object as from argparse.ArgumentParser.parse_args()
  """
  parser, input_opts, output_opts = resource_utils.ResourceArgsParser()

  input_opts.add_argument(
      '--aapt2-path', required=True, help='Path to the Android aapt2 tool.')
  input_opts.add_argument(
      '--android-manifest', required=True, help='AndroidManifest.xml path.')
  input_opts.add_argument(
      '--android-manifest-expected',
      help='Expected contents for the final manifest.')
  input_opts.add_argument(
      '--android-manifest-normalized', help='Normalized manifest.')
  input_opts.add_argument(
      '--fail-if-unexpected-android-manifest',
      action='store_true',
      help=
      'Fail if expected manifest contents do not match final manifest contents.'
  )
  group = input_opts.add_mutually_exclusive_group()
  group.add_argument(
      '--shared-resources',
      action='store_true',
      help='Make all resources in R.java non-final and allow the resource IDs '
      'to be reset to a different package index when the apk is loaded by '
      'another application at runtime.')
  group.add_argument(
      '--app-as-shared-lib',
      action='store_true',
      help='Same as --shared-resources, but also ensures all resource IDs are '
      'directly usable from the APK loaded as an application.')

  input_opts.add_argument(
      '--package-id',
      help='Custom package ID for resources (instead of 0x7f). Cannot be used '
      'with --shared-resources.')

  input_opts.add_argument(
      '--package-name-to-id-mapping',
      help='List containing mapping from package name to package IDs that will '
      'be assigned.')

  input_opts.add_argument(
      '--package-name',
      help='Package name that will be used to determine package ID.')

  input_opts.add_argument(
      '--rename-manifest-package', help='Package name to force AAPT to use.')

  input_opts.add_argument(
      '--arsc-package-name',
      help='Package name to set in manifest of resources.arsc file. This is '
      'only used for apks under test.')

  input_opts.add_argument(
      '--shared-resources-whitelist',
      help='An R.txt file acting as a whitelist for resources that should be '
           'non-final and have their package ID changed at runtime in R.java. '
           'Implies and overrides --shared-resources.')

  input_opts.add_argument(
      '--shared-resources-whitelist-locales',
      default='[]',
      help='Optional GN-list of locales. If provided, all strings corresponding'
      ' to this locale list will be kept in the final output for the '
      'resources identified through --shared-resources-whitelist, even '
      'if --locale-whitelist is being used.')

  input_opts.add_argument(
      '--use-resource-ids-path',
      help='Use resource IDs generated by aapt --emit-ids.')

  input_opts.add_argument(
      '--support-zh-hk',
      action='store_true',
      help='Use zh-rTW resources for zh-rHK.')

  input_opts.add_argument(
      '--debuggable',
      action='store_true',
      help='Whether to add android:debuggable="true".')

  input_opts.add_argument('--version-code', help='Version code for apk.')
  input_opts.add_argument('--version-name', help='Version name for apk.')
  input_opts.add_argument(
      '--min-sdk-version', required=True, help='android:minSdkVersion for APK.')
  input_opts.add_argument(
      '--target-sdk-version',
      required=True,
      help="android:targetSdkVersion for APK.")
  input_opts.add_argument(
      '--max-sdk-version', help="android:maxSdkVersion for APK.")

  input_opts.add_argument(
      '--locale-whitelist',
      default='[]',
      help='GN list of languages to include. All other language configs will '
          'be stripped out. List may include a combination of Android locales '
          'or Chrome locales.')

  input_opts.add_argument('--resource-blacklist-regex', default='',
                          help='Do not include matching drawables.')

  input_opts.add_argument(
      '--resource-blacklist-exceptions',
      default='[]',
      help='GN list of globs that say which blacklisted images to include even '
           'when --resource-blacklist-regex is set.')

  input_opts.add_argument('--png-to-webp', action='store_true',
                          help='Convert png files to webp format.')

  input_opts.add_argument('--webp-binary', default='',
                          help='Path to the cwebp binary.')

  input_opts.add_argument(
      '--no-xml-namespaces',
      action='store_true',
      help='Whether to strip xml namespaces from processed xml resources.')
  input_opts.add_argument(
      '--short-resource-paths',
      action='store_true',
      help='Whether to shorten resource paths inside the apk or module.')
  input_opts.add_argument(
      '--strip-resource-names',
      action='store_true',
      help='Whether to strip resource names from the resource table of the apk '
      'or module.')

  output_opts.add_argument('--arsc-path', help='Apk output for arsc format.')
  output_opts.add_argument('--proto-path', help='Apk output for proto format.')
  group = input_opts.add_mutually_exclusive_group()
  group.add_argument(
      '--optimized-arsc-path',
      help='Output for `aapt2 optimize` for arsc format (enables the step).')
  group.add_argument(
      '--optimized-proto-path',
      help='Output for `aapt2 optimize` for proto format (enables the step).')
  input_opts.add_argument(
      '--resources-config-path', help='Path to aapt2 resources config file.')

  output_opts.add_argument(
      '--info-path', help='Path to output info file for the partial apk.')

  output_opts.add_argument(
      '--srcjar-out',
      required=True,
      help='Path to srcjar to contain generated R.java.')

  output_opts.add_argument('--r-text-out',
                           help='Path to store the generated R.txt file.')

  output_opts.add_argument(
      '--proguard-file', help='Path to proguard.txt generated file.')

  output_opts.add_argument(
      '--proguard-file-main-dex',
      help='Path to proguard.txt generated file for main dex.')

  output_opts.add_argument(
      '--emit-ids-out', help='Path to file produced by aapt2 --emit-ids.')

  output_opts.add_argument(
      '--resources-path-map-out-path',
      help='Path to file produced by aapt2 that maps original resource paths '
      'to shortened resource paths inside the apk or module.')

  options = parser.parse_args(args)

  resource_utils.HandleCommonOptions(options)

  options.locale_whitelist = build_utils.ParseGnList(options.locale_whitelist)
  options.shared_resources_whitelist_locales = build_utils.ParseGnList(
      options.shared_resources_whitelist_locales)
  options.resource_blacklist_exceptions = build_utils.ParseGnList(
      options.resource_blacklist_exceptions)

  if options.optimized_proto_path and not options.proto_path:
    # We could write to a temp file, but it's simpler to require it.
    parser.error('--optimized-proto-path requires --proto-path')

  if not options.arsc_path and not options.proto_path:
    parser.error('One of --arsc-path or --proto-path is required.')

  if options.resources_path_map_out_path and not options.short_resource_paths:
    parser.error(
        '--resources-path-map-out-path requires --short-resource-paths')

  if options.package_name_to_id_mapping:
    package_names_list = build_utils.ParseGnList(
        options.package_name_to_id_mapping)
    options.package_name_to_id_mapping = _ListToDictionary(
        package_names_list, '=')

  return options


def _SortZip(original_path, sorted_path):
  """Generate new zip archive by sorting all files in the original by name."""
  with zipfile.ZipFile(sorted_path, 'w') as sorted_zip, \
      zipfile.ZipFile(original_path, 'r') as original_zip:
    for info in sorted(original_zip.infolist(), key=lambda i: i.filename):
      sorted_zip.writestr(info, original_zip.read(info))


def _IterFiles(root_dir):
  for root, _, files in os.walk(root_dir):
    for f in files:
      yield os.path.join(root, f)


def _DuplicateZhResources(resource_dirs):
  """Duplicate Taiwanese resources into Hong-Kong specific directory."""
  renamed_paths = dict()
  for resource_dir in resource_dirs:
    # We use zh-TW resources for zh-HK (if we have zh-TW resources).
    for path in _IterFiles(resource_dir):
      if 'zh-rTW' in path:
        hk_path = path.replace('zh-rTW', 'zh-rHK')
        build_utils.MakeDirectory(os.path.dirname(hk_path))
        shutil.copyfile(path, hk_path)
        renamed_paths[os.path.relpath(hk_path, resource_dir)] = os.path.relpath(
            path, resource_dir)
  return renamed_paths


def _RenameLocaleResourceDirs(resource_dirs):
  """Rename locale resource directories into standard names when necessary.

  This is necessary to deal with the fact that older Android releases only
  support ISO 639-1 two-letter codes, and sometimes even obsolete versions
  of them.

  In practice it means:
    * 3-letter ISO 639-2 qualifiers are renamed under a corresponding
      2-letter one. E.g. for Filipino, strings under values-fil/ will be moved
      to a new corresponding values-tl/ sub-directory.

    * Modern ISO 639-1 codes will be renamed to their obsolete variant
      for Indonesian, Hebrew and Yiddish (e.g. 'values-in/ -> values-id/).

    * Norwegian macrolanguage strings will be renamed to Bokmål (main
      Norway language). See http://crbug.com/920960. In practice this
      means that 'values-no/ -> values-nb/' unless 'values-nb/' already
      exists.

    * BCP 47 langauge tags will be renamed to an equivalent ISO 639-1
      locale qualifier if possible (e.g. 'values-b+en+US/ -> values-en-rUS').
      Though this is not necessary at the moment, because no third-party
      package that Chromium links against uses these for the current list of
      supported locales, this may change when the list is extended in the
      future).

  Args:
    resource_dirs: list of top-level resource directories.
  Returns:
    A dictionary mapping renamed paths to their original location
    (e.g. '.../values-tl/strings.xml' -> ' .../values-fil/strings.xml').
  """
  renamed_paths = dict()
  for resource_dir in resource_dirs:
    for path in _IterFiles(resource_dir):
      locale = resource_utils.FindLocaleInStringResourceFilePath(path)
      if not locale:
        continue
      cr_locale = resource_utils.ToChromiumLocaleName(locale)
      if not cr_locale:
        continue  # Unsupported Android locale qualifier!?
      locale2 = resource_utils.ToAndroidLocaleName(cr_locale)
      if locale != locale2:
        path2 = path.replace('/values-%s/' % locale, '/values-%s/' % locale2)
        if path == path2:
          raise Exception('Could not substitute locale %s for %s in %s' %
                          (locale, locale2, path))
        if os.path.exists(path2):
          # This happens sometimes, e.g. some libraries provide both
          # values-nb/ and values-no/ with the same content.
          continue
        build_utils.MakeDirectory(os.path.dirname(path2))
        shutil.move(path, path2)
        renamed_paths[os.path.relpath(path2, resource_dir)] = os.path.relpath(
            path, resource_dir)
  return renamed_paths


def _ToAndroidLocales(locale_whitelist, support_zh_hk):
  """Converts the list of Chrome locales to Android config locale qualifiers.

  Args:
    locale_whitelist: A list of Chromium locale names.
    support_zh_hk: True if we need to support zh-HK by duplicating
      the zh-TW strings.
  Returns:
    A set of matching Android config locale qualifier names.
  """
  ret = set()
  for locale in locale_whitelist:
    locale = resource_utils.ToAndroidLocaleName(locale)
    if locale is None or ('-' in locale and '-r' not in locale):
      raise Exception('Unsupported Chromium locale name: %s' % locale)
    ret.add(locale)
    # Always keep non-regional fall-backs.
    language = locale.split('-')[0]
    ret.add(language)

  # We don't actually support zh-HK in Chrome on Android, but we mimic the
  # native side behavior where we use zh-TW resources when the locale is set to
  # zh-HK. See https://crbug.com/780847.
  if support_zh_hk:
    assert not any('HK' in l for l in locale_whitelist), (
        'Remove special logic if zh-HK is now supported (crbug.com/780847).')
    ret.add('zh-rHK')
  return set(ret)


def _MoveImagesToNonMdpiFolders(res_root):
  """Move images from drawable-*-mdpi-* folders to drawable-* folders.

  Why? http://crbug.com/289843
  """
  renamed_paths = dict()
  for src_dir_name in os.listdir(res_root):
    src_components = src_dir_name.split('-')
    if src_components[0] != 'drawable' or 'mdpi' not in src_components:
      continue
    src_dir = os.path.join(res_root, src_dir_name)
    if not os.path.isdir(src_dir):
      continue
    dst_components = [c for c in src_components if c != 'mdpi']
    assert dst_components != src_components
    dst_dir_name = '-'.join(dst_components)
    dst_dir = os.path.join(res_root, dst_dir_name)
    build_utils.MakeDirectory(dst_dir)
    for src_file_name in os.listdir(src_dir):
      if not os.path.splitext(src_file_name)[1] in ('.png', '.webp'):
        continue
      src_file = os.path.join(src_dir, src_file_name)
      dst_file = os.path.join(dst_dir, src_file_name)
      assert not os.path.lexists(dst_file)
      shutil.move(src_file, dst_file)
      renamed_paths[os.path.relpath(dst_file, res_root)] = os.path.relpath(
          src_file, res_root)
  return renamed_paths


def _PackageIdFromOptions(options):
  package_id = None
  if options.package_id:
    package_id = options.package_id
  if options.package_name:
    package_id = options.package_name_to_id_mapping.get(options.package_name)
    if package_id is None:
      raise Exception(
          'Package name %s is not present in package_name_to_id_mapping.' %
          options.package_name)
  return package_id


def _FixManifest(options, temp_dir):
  """Fix the APK's AndroidManifest.xml.

  This adds any missing namespaces for 'android' and 'tools', and
  sets certains elements like 'platformBuildVersionCode' or
  'android:debuggable' depending on the content of |options|.

  Args:
    options: The command-line arguments tuple.
    temp_dir: A temporary directory where the fixed manifest will be written to.
  Returns:
    Tuple of:
     * Manifest path within |temp_dir|.
     * Original package_name.
  """
  def maybe_extract_version(j):
    try:
      return resource_utils.ExtractBinaryManifestValues(options.aapt2_path, j)
    except build_utils.CalledProcessError:
      return None

  android_sdk_jars = [j for j in options.include_resources
                      if os.path.basename(j) in ('android.jar',
                                                 'android_system.jar')]
  extract_all = [maybe_extract_version(j) for j in android_sdk_jars]
  successful_extractions = [x for x in extract_all if x]
  if len(successful_extractions) == 0:
    raise Exception(
        'Unable to find android SDK jar among candidates: %s'
            % ', '.join(android_sdk_jars))
  elif len(successful_extractions) > 1:
    raise Exception(
        'Found multiple android SDK jars among candidates: %s'
            % ', '.join(android_sdk_jars))
  version_code, version_name = successful_extractions.pop()[:2]

  debug_manifest_path = os.path.join(temp_dir, 'AndroidManifest.xml')
  doc, manifest_node, app_node = manifest_utils.ParseManifest(
      options.android_manifest)

  manifest_utils.AssertUsesSdk(manifest_node, options.min_sdk_version,
                               options.target_sdk_version,
                               options.max_sdk_version)

  manifest_node.set('platformBuildVersionCode', version_code)
  manifest_node.set('platformBuildVersionName', version_name)

  orig_package = manifest_node.get('package')
  if options.arsc_package_name:
    manifest_node.set('package', options.arsc_package_name)

  if options.debuggable:
    app_node.set('{%s}%s' % (manifest_utils.ANDROID_NAMESPACE, 'debuggable'),
                 'true')

  manifest_utils.SaveManifest(doc, debug_manifest_path)
  return debug_manifest_path, orig_package


def _VerifyManifest(actual_manifest, expected_manifest, normalized_manifest,
                    fail_if_unexpected_manifest):
  with build_utils.AtomicOutput(normalized_manifest) as normalized_output:
    normalized_output.write(manifest_utils.NormalizeManifest(actual_manifest))
  msg = diff_utils.DiffFileContents(expected_manifest, normalized_manifest)
  if msg:
    sys.stderr.write("""\
AndroidManifest.xml expectations file needs updating. For details see:
https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/android/java/README.md
""")
    sys.stderr.write(msg)
    if fail_if_unexpected_manifest:
      sys.exit(1)


def _ResourceNameFromPath(path):
  return os.path.splitext(os.path.basename(path))[0]


def _CreateKeepPredicate(resource_dirs, resource_blacklist_regex,
                         resource_blacklist_exceptions):
  """Return a predicate lambda to determine which resource files to keep.

  Args:
    resource_dirs: list of top-level resource directories.
    resource_blacklist_regex: A regular expression describing all resources
      to exclude, except if they are mip-maps, or if they are listed
      in |resource_blacklist_exceptions|.
    resource_blacklist_exceptions: A list of glob patterns corresponding
      to exceptions to the |resource_blacklist_regex|.
  Returns:
    A lambda that takes a path, and returns true if the corresponding file
    must be kept.
  """
  naive_predicate = lambda path: os.path.basename(path)[0] != '.'
  if resource_blacklist_regex == '':
    # Do not extract dotfiles (e.g. ".gitkeep"). aapt ignores them anyways.
    return naive_predicate

  if resource_blacklist_regex != '':
    # A simple predicate that only removes (returns False for) paths covered by
    # the blacklist regex, except if they are mipmaps, or listed as exceptions.
    naive_predicate = lambda path: (
        not re.search(resource_blacklist_regex, path) or
        re.search(r'[/-]mipmap[/-]', path) or
        build_utils.MatchesGlob(path, resource_blacklist_exceptions))

  # Build a set of all names from drawables kept by naive_predicate().
  # Used later to ensure that we never exclude drawables from densities
  # that are filtered-out by naive_predicate().
  non_filtered_drawables = set()
  for resource_dir in resource_dirs:
    for path in _IterFiles(resource_dir):
      if re.search(r'[/-]drawable[/-]', path) and naive_predicate(path):
        non_filtered_drawables.add(_ResourceNameFromPath(path))

  # NOTE: Defined as a function, instead of a lambda to avoid the
  # auto-formatter to put this on a very long line that overflows.
  def drawable_predicate(path):
    return (naive_predicate(path)
            or _ResourceNameFromPath(path) not in non_filtered_drawables)

  return drawable_predicate


def _ConvertToWebP(webp_binary, png_files):
  renamed_paths = dict()
  pool = multiprocessing.pool.ThreadPool(10)
  def convert_image(png_path_tuple):
    png_path, original_dir = png_path_tuple
    root = os.path.splitext(png_path)[0]
    webp_path = root + '.webp'
    args = [webp_binary, png_path, '-mt', '-quiet', '-m', '6', '-q', '100',
        '-lossless', '-o', webp_path]
    subprocess.check_call(args)
    os.remove(png_path)
    renamed_paths[os.path.relpath(webp_path, original_dir)] = os.path.relpath(
        png_path, original_dir)

  pool.map(convert_image, [f for f in png_files
                           if not _PNG_WEBP_BLACKLIST_PATTERN.match(f[0])])
  pool.close()
  pool.join()
  return renamed_paths


def _CompileDeps(aapt2_path, dep_subdirs, temp_dir):
  partials_dir = os.path.join(temp_dir, 'partials')
  build_utils.MakeDirectory(partials_dir)
  partial_compile_command = [
      aapt2_path,
      'compile',
      # TODO(wnwen): Turn this on once aapt2 forces 9-patch to be crunched.
      # '--no-crunch',
  ]
  pool = multiprocessing.pool.ThreadPool(10)
  def compile_partial(directory):
    dirname = os.path.basename(directory)
    partial_path = os.path.join(partials_dir, dirname + '.zip')
    compile_command = (partial_compile_command +
                       ['--dir', directory, '-o', partial_path])
    # There are resources targeting API-versions lower than our minapi. For
    # various reasons it's easier to let aapt2 ignore these than for us to
    # remove them from our build (e.g. it's from a 3rd party library).
    build_utils.CheckOutput(
        compile_command,
        stderr_filter=lambda output:
            build_utils.FilterLines(
                output, r'ignoring configuration .* for (styleable|attribute)'))

    # Sorting the files in the partial ensures deterministic output from the
    # aapt2 link step which uses order of files in the partial.
    sorted_partial_path = os.path.join(partials_dir, dirname + '.sorted.zip')
    _SortZip(partial_path, sorted_partial_path)

    return sorted_partial_path

  partials = pool.map(compile_partial, dep_subdirs)
  pool.close()
  pool.join()
  return partials


def _CreateResourceInfoFile(renamed_paths, info_path, dependencies_res_zips):
  lines = set()
  for zip_file in dependencies_res_zips:
    zip_info_file_path = zip_file + '.info'
    if os.path.exists(zip_info_file_path):
      with open(zip_info_file_path, 'r') as zip_info_file:
        lines.update(zip_info_file.readlines())
  for dest, source in renamed_paths.iteritems():
    lines.add('Rename:{},{}\n'.format(dest, source))
  with open(info_path, 'w') as info_file:
    info_file.writelines(sorted(lines))


def _RemoveUnwantedLocalizedStrings(dep_subdirs, options):
  """Remove localized strings that should not go into the final output.

  Args:
    dep_subdirs: List of resource dependency directories.
    options: Command-line options namespace.
  """
  if (not options.locale_whitelist
      and not options.shared_resources_whitelist_locales):
    # Keep everything, there is nothing to do.
    return

  # Collect locale and file paths from the existing subdirs.
  # The following variable maps Android locale names to
  # sets of corresponding xml file paths.
  locale_to_files_map = collections.defaultdict(set)
  for directory in dep_subdirs:
    for f in _IterFiles(directory):
      locale = resource_utils.FindLocaleInStringResourceFilePath(f)
      if locale:
        locale_to_files_map[locale].add(f)

  all_locales = set(locale_to_files_map)

  # Set A: wanted locales, either all of them or the
  # list provided by --locale-whitelist.
  wanted_locales = all_locales
  if options.locale_whitelist:
    wanted_locales = _ToAndroidLocales(options.locale_whitelist,
                                       options.support_zh_hk)

  # Set B: shared resources locales, which is either set A
  # or the list provided by --shared-resources-whitelist-locales
  shared_resources_locales = wanted_locales
  shared_names_whitelist = set()
  if options.shared_resources_whitelist_locales:
    shared_names_whitelist = set(
        resource_utils.GetRTxtStringResourceNames(
            options.shared_resources_whitelist))

    shared_resources_locales = _ToAndroidLocales(
        options.shared_resources_whitelist_locales, options.support_zh_hk)

  # Remove any file that belongs to a locale not covered by
  # either A or B.
  removable_locales = (all_locales - wanted_locales - shared_resources_locales)
  for locale in removable_locales:
    for path in locale_to_files_map[locale]:
      os.remove(path)

  # For any locale in B but not in A, only keep the shared
  # resource strings in each file.
  for locale in shared_resources_locales - wanted_locales:
    for path in locale_to_files_map[locale]:
      resource_utils.FilterAndroidResourceStringsXml(
          path, lambda x: x in shared_names_whitelist)

  # For any locale in A but not in B, only keep the strings
  # that are _not_ from shared resources in the file.
  for locale in wanted_locales - shared_resources_locales:
    for path in locale_to_files_map[locale]:
      resource_utils.FilterAndroidResourceStringsXml(
          path, lambda x: x not in shared_names_whitelist)


def _PackageApk(options, build):
  """Compile and link resources with aapt2.

  Args:
    options: The command-line options.
    build: BuildContext object.
  """
  dep_subdirs = resource_utils.ExtractDeps(options.dependencies_res_zips,
                                           build.deps_dir)
  renamed_paths = dict()
  renamed_paths.update(_DuplicateZhResources(dep_subdirs))
  renamed_paths.update(_RenameLocaleResourceDirs(dep_subdirs))

  _RemoveUnwantedLocalizedStrings(dep_subdirs, options)

  # Create a function that selects which resource files should be packaged
  # into the final output. Any file that does not pass the predicate will
  # be removed below.
  keep_predicate = _CreateKeepPredicate(dep_subdirs,
                                        options.resource_blacklist_regex,
                                        options.resource_blacklist_exceptions)
  png_paths = []
  for directory in dep_subdirs:
    for f in _IterFiles(directory):
      if not keep_predicate(f):
        os.remove(f)
      elif f.endswith('.png'):
        png_paths.append((f, directory))
  if png_paths and options.png_to_webp:
    renamed_paths.update(_ConvertToWebP(options.webp_binary, png_paths))
  for directory in dep_subdirs:
    renamed_paths.update(_MoveImagesToNonMdpiFolders(directory))

  _CreateResourceInfoFile(renamed_paths, build.info_path,
                          options.dependencies_res_zips)

  link_command = [
      options.aapt2_path,
      'link',
      '--auto-add-overlay',
      '--no-version-vectors',
      # Set SDK versions in case they are not set in the Android manifest.
      '--min-sdk-version',
      options.min_sdk_version,
      '--target-sdk-version',
      options.target_sdk_version,
  ]

  for j in options.include_resources:
    link_command += ['-I', j]
  if options.version_code:
    link_command += ['--version-code', options.version_code]
  if options.version_name:
    link_command += ['--version-name', options.version_name]
  if options.proguard_file:
    link_command += ['--proguard', build.proguard_path]
  if options.proguard_file_main_dex:
    link_command += ['--proguard-main-dex', build.proguard_main_dex_path]
  if options.emit_ids_out:
    link_command += ['--emit-ids', build.emit_ids_path]
  if options.r_text_in:
    shutil.copyfile(options.r_text_in, build.r_txt_path)
  else:
    link_command += ['--output-text-symbols', build.r_txt_path]

  # Note: only one of --proto-format, --shared-lib or --app-as-shared-lib
  #       can be used with recent versions of aapt2.
  if options.shared_resources and not options.proto_path:
    link_command.append('--shared-lib')

  if options.no_xml_namespaces:
    link_command.append('--no-xml-namespaces')

  package_id = _PackageIdFromOptions(options)
  if package_id is not None:
    link_command += ['--package-id', package_id, '--allow-reserved-package-id']

  fixed_manifest, desired_manifest_package_name = _FixManifest(
      options, build.temp_dir)
  if options.rename_manifest_package:
    desired_manifest_package_name = options.rename_manifest_package
  if options.android_manifest_expected:
    _VerifyManifest(fixed_manifest, options.android_manifest_expected,
                    options.android_manifest_normalized,
                    options.fail_if_unexpected_android_manifest)

  link_command += [
      '--manifest', fixed_manifest, '--rename-manifest-package',
      desired_manifest_package_name
  ]

  # Creates a .zip with AndroidManifest.xml, resources.arsc, res/*
  # Also creates R.txt
  if options.use_resource_ids_path:
    _CreateStableIdsFile(options.use_resource_ids_path, build.stable_ids_path,
                         desired_manifest_package_name)
    link_command += ['--stable-ids', build.stable_ids_path]

  partials = _CompileDeps(options.aapt2_path, dep_subdirs, build.temp_dir)
  for partial in partials:
    link_command += ['-R', partial]

  if options.proto_path:
    link_command += ['--proto-format', '-o', build.proto_path]
  else:
    link_command += ['-o', build.arsc_path]

  build_utils.CheckOutput(link_command, print_stdout=False, print_stderr=False)

  if options.proto_path and options.arsc_path:
    build_utils.CheckOutput([
        options.aapt2_path, 'convert', '-o', build.arsc_path, build.proto_path
    ])

  if options.optimized_proto_path:
    _OptimizeApk(build.optimized_proto_path, options, build.temp_dir,
                 build.proto_path, build.r_txt_path)
  elif options.optimized_arsc_path:
    _OptimizeApk(build.optimized_arsc_path, options, build.temp_dir,
                 build.arsc_path, build.r_txt_path)


def _OptimizeApk(output, options, temp_dir, unoptimized_path, r_txt_path):
  """Optimize intermediate .ap_ file with aapt2.

  Args:
    output: Path to write to.
    options: The command-line options.
    temp_dir: A temporary directory.
    unoptimized_path: path of the apk to optimize.
    r_txt_path: path to the R.txt file of the unoptimized apk.
  """
  optimize_command = [
      options.aapt2_path,
      'optimize',
      unoptimized_path,
      '-o',
      output,
  ]

  # Optimize the resources.arsc file by obfuscating resource names and only
  # allow usage via R.java constant.
  if options.strip_resource_names:
    # Resources of type ID are references to UI elements/views. They are used by
    # UI automation testing frameworks. They are kept in so that they dont break
    # tests, even though they may not actually be used during runtime. See
    # https://crbug.com/900993
    id_resources = _ExtractIdResources(r_txt_path)
    gen_config_path = os.path.join(temp_dir, 'aapt2.config')
    if options.resources_config_path:
      shutil.copyfile(options.resources_config_path, gen_config_path)
    with open(gen_config_path, 'a+') as config:
      for resource in id_resources:
        config.write('{}#no_obfuscate\n'.format(resource))

    optimize_command += [
        '--enable-resource-obfuscation',
        '--resources-config-path',
        gen_config_path,
    ]

  if options.short_resource_paths:
    optimize_command += ['--enable-resource-path-shortening']
  if options.resources_path_map_out_path:
    optimize_command += [
        '--resource-path-shortening-map', options.resources_path_map_out_path
    ]

  build_utils.CheckOutput(
      optimize_command, print_stdout=False, print_stderr=False)


def _ExtractIdResources(rtxt_path):
  """Extract resources of type ID from the R.txt file

  Args:
    rtxt_path: Path to R.txt file with all the resources
  Returns:
    List of id resources in the form of id/<resource_name>
  """
  id_resources = []
  with open(rtxt_path) as rtxt:
    for line in rtxt:
      if ' id ' in line:
        resource_name = line.split()[2]
        id_resources.append('id/{}'.format(resource_name))
  return id_resources


@contextlib.contextmanager
def _CreateStableIdsFile(in_path, out_path, package_name):
  """Transforms a file generated by --emit-ids from another package.

  --stable-ids is generally meant to be used by different versions of the same
  package. To make it work for other packages, we need to transform the package
  name references to match the package that resources are being generated for.

  Note: This will fail if the package ID of the resources in
  |options.use_resource_ids_path| does not match the package ID of the
  resources being linked.
  """
  with open(in_path) as stable_ids_file:
    with open(out_path, 'w') as output_ids_file:
      output_stable_ids = re.sub(
          r'^.*?:',
          package_name + ':',
          stable_ids_file.read(),
          flags=re.MULTILINE)
      output_ids_file.write(output_stable_ids)


def _WriteOutputs(options, build):
  possible_outputs = [
      (options.srcjar_out, build.srcjar_path),
      (options.r_text_out, build.r_txt_path),
      (options.arsc_path, build.arsc_path),
      (options.proto_path, build.proto_path),
      (options.optimized_arsc_path, build.optimized_arsc_path),
      (options.optimized_proto_path, build.optimized_proto_path),
      (options.proguard_file, build.proguard_path),
      (options.proguard_file_main_dex, build.proguard_main_dex_path),
      (options.emit_ids_out, build.emit_ids_path),
      (options.info_path, build.info_path),
  ]

  for final, temp in possible_outputs:
    # Write file only if it's changed.
    if final and not (os.path.exists(final) and filecmp.cmp(final, temp)):
      shutil.move(temp, final)


def main(args):
  args = build_utils.ExpandFileArgs(args)
  options = _ParseArgs(args)

  debug_temp_resources_dir = os.environ.get(_ENV_DEBUG_VARIABLE)
  if debug_temp_resources_dir:
    debug_temp_resources_dir = os.path.join(debug_temp_resources_dir,
                                            os.path.basename(options.arsc_path))
    build_utils.DeleteDirectory(debug_temp_resources_dir)
    build_utils.MakeDirectory(debug_temp_resources_dir)

  with resource_utils.BuildContext(debug_temp_resources_dir) as build:
    _PackageApk(options, build)

    # If --shared-resources-whitelist is used, the all resources listed in
    # the corresponding R.txt file will be non-final, and an onResourcesLoaded()
    # will be generated to adjust them at runtime.
    #
    # Otherwise, if --shared-resources is used, the all resources will be
    # non-final, and an onResourcesLoaded() method will be generated too.
    #
    # Otherwise, all resources will be final, and no method will be generated.
    #
    rjava_build_options = resource_utils.RJavaBuildOptions()
    if options.shared_resources_whitelist:
      rjava_build_options.ExportSomeResources(
          options.shared_resources_whitelist)
      rjava_build_options.GenerateOnResourcesLoaded()
    elif options.shared_resources or options.app_as_shared_lib:
      rjava_build_options.ExportAllResources()
      rjava_build_options.GenerateOnResourcesLoaded()

    custom_root_package_name = None
    grandparent_custom_package_name = None

    if options.arsc_package_name:
      # This is for test apks with an apk under test. If we have an apk under
      # test we don't want to name the resources for both the test apk and the
      # under test apk "base". This special case lets us name the test apk's
      # resources "test".
      custom_root_package_name = 'test'
    elif options.package_name:
      # If there exists a custom package name such as vr for a feature module,
      # pass the name and base for the parent_custom_root_package_name.
      custom_root_package_name = options.package_name
      grandparent_custom_package_name = 'base'
    else:
      # No grandparent_custom_package_name for base module
      custom_root_package_name = 'base'

    resource_utils.CreateRJavaFiles(
        build.srcjar_dir, None, build.r_txt_path, options.extra_res_packages,
        options.extra_r_text_files, rjava_build_options, options.srcjar_out,
        custom_root_package_name, grandparent_custom_package_name)

    build_utils.ZipDir(build.srcjar_path, build.srcjar_dir)

    # Sanity check that the created resources have the expected package ID.
    expected_id = _PackageIdFromOptions(options)
    if expected_id is None:
      expected_id = '0x00' if options.shared_resources else '0x7f'
    expected_id = int(expected_id, 16)
    _, package_id = resource_utils.ExtractArscPackage(
        options.aapt2_path,
        build.arsc_path if options.arsc_path else build.proto_path)
    if package_id != expected_id:
      raise Exception(
          'Invalid package ID 0x%x (expected 0x%x)' % (package_id, expected_id))

    _WriteOutputs(options, build)

  if options.depfile:
    build_utils.WriteDepfile(
        options.depfile,
        options.srcjar_out,
        inputs=options.dependencies_res_zips + options.extra_r_text_files,
        add_pydeps=False)


if __name__ == '__main__':
  main(sys.argv[1:])
