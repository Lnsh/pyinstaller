#-----------------------------------------------------------------------------
# Copyright (c) 2005-2015, PyInstaller Development Team.
#
# Distributed under the terms of the GNU General Public License with exception
# for distributing bootloader.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------


import copy
import glob
import os
import pytest
import subprocess
import sys

from PyInstaller import compat, configure
from PyInstaller import main as pyi_main
from PyInstaller.compat import is_darwin, is_win, is_py2, safe_repr
from PyInstaller.depend.analysis import initialize_modgraph
from PyInstaller.utils.win32 import winutils


# Directory with Python scripts for functional tests. E.g. main scripts, etc.
_SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
# Directory with testing modules used in some tests.
_MODULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'modules')


class AppBuilder(object):

    def __init__(self, tmpdir, bundle_mode, module_graph):
        self._tmpdir = tmpdir
        self._mode = bundle_mode
        self._specdir = self._tmpdir
        self._distdir = os.path.join(self._tmpdir, 'dist')
        self._builddir = os.path.join(self._tmpdir, 'build')
        self._modgraph = module_graph

    def test_script(self, script, pyi_args=[], app_name=None, app_args=[], runtime=None):
        """
        Main method to wrap all phases of testing a Python script.

        :param script: Name of script to create executable from.
        :param pyi_args: Additional arguments to pass to PyInstaller when creating executable.
        :param app_name: Name of the executable. This is equivalent to argument --name=APPNAME.
        :param app_args: Additional arguments to pass to
        :param runtime: Time in milliseconds how long to keep executable running.
        """
        if app_name:
            pyi_args.extend(['--name', app_name])
        else:
            # Derive name from script name.
            app_name = os.path.splitext(os.path.basename(script))[0]

        self.script = os.path.join(_SCRIPT_DIR, script)
        assert os.path.exists(self.script), 'Script %s not found.' % script
        self.toc_files = None

        assert self._test_building(args=pyi_args), 'Building of %s failed.' % script
        self._test_executables(app_name, args=app_args, runtime=runtime)
        # TODO implement examining toc files for multipackage tests.
        assert self._test_created_files(), 'Matching .toc of %s failed.' % script

    def _test_executables(self, name, args, runtime):
        """
        Run created executable to make sure it works.

        Multipackage-tests generate more than one exe-file and all of
        them have to be run.

        :param args: CLI options to pass to the created executable.
        :param runtime: Time in miliseconds how long to keep the executable running.

        :return: Exit code of the executable.
        """
        # TODO implement runtime - kill the app (Ctrl+C) when time times out
        exes = self._find_executables(name)
        # Empty list means that PyInstaller probably failed to create any executable.
        assert exes != [], 'No executable file was found.'
        for exe in exes:
            retcode = self._run_executable(exe, args)
            assert retcode == 0, 'Running exe %s failed with return-code %s.' % (exe, retcode)

    def _test_created_files(self):
        """
        Examine files that were created by PyInstaller.

        :return: True if everything goes well False otherwise.
        """
        # TODO implement examining toc files for multipackage tests.
        if self.toc_files:
            return self._test_logs()
        return True

    def _find_executables(self, name):
        """
        Search for all executables generated by the testcase.

        If the test-case is called e.g. 'test_multipackage1', this is
        searching for each of 'test_multipackage1.exe' and
        'multipackage1_?.exe' in both one-file- and one-dir-mode.

        :param name: Name of the executable to look for.

        :return: List of executables
        """
        exes = []
        onedir_pt = os.path.join(self._distdir, name, name)
        onefile_pt = os.path.join(self._distdir, name)
        patterns = [onedir_pt, onefile_pt,
                    # Multipackage one-dir
                    onedir_pt + '_?',
                    # Multipackage one-file
                    onefile_pt + '_?']
        # For Windows append .exe extension to patterns.
        if is_win:
            patterns = [pt + '.exe' for pt in patterns]
        # For Mac OS X append pattern for .app bundles.
        if is_darwin:
            # e.g:  ./dist/name.app/Contents/MacOS/name
            pt = os.path.join(self._distdir, name + '.app', 'Contents', 'MacOS', name)
            patterns.append(pt)
        # Apply file patterns.
        for pattern in patterns:
            for prog in glob.glob(pattern):
                if os.path.isfile(prog):
                    exes.append(prog)
        return exes

    def _run_executable(self, prog, args):
        """
        Run executable created by PyInstaller.

        :param args: CLI options to pass to the created executable.
        """
        # Run the test in a clean environment to make sure they're really self-contained.
        prog_env = copy.deepcopy(os.environ)
        prog_env['PATH'] = ''
        del prog_env['PATH']
        # For Windows we need to keep minimal PATH for successful running of some tests.
        if is_win:
            # Minimum Windows PATH is in most cases:   C:\Windows\system32;C:\Windows
            prog_env['PATH'] = os.pathsep.join(winutils.get_system_path())

        # Run executable in the directory where it is.
        prog_cwd = os.path.dirname(prog)

        # On Windows, `subprocess.call` does not search in its `cwd` for the
        # executable named as the first argument, so it must be passed as an
        # absolute path. This is documented for the Windows API `CreateProcess`
        if not is_win:
            # The executable will be called as relative not absolute path.
            prog = os.path.join(os.curdir, os.path.basename(prog))

        # Workaround to enable win_codepage_test
        # If _distdir is 'bytes', PyI build fails with ASCII decode error
        # when it joins the 'bytes' _distdir with the 'unicode' filenames from bindep and
        # winmanifest.
        #
        # PyI succeeds with _distdir as 'unicode', but subprocess
        # fails with ASCII encode error. subprocess succeeds if progname is
        # mbcs-encoded 'bytes'
        if is_win and is_py2:
            if isinstance(prog, unicode):
                prog = prog.encode('mbcs')
            if isinstance(prog_cwd, unicode):
                prog_cwd = prog_cwd.encode('mbcs')

        # Run executable. stderr is redirected to stdout.
        print('RUNNING: ' + safe_repr(prog))
        # Using sys.stdout/sys.stderr for subprocess fixes printing messages in
        # Windows command prompt. Py.test is then able to collect stdout/sterr
        # messages and display them if a test fails.
        retcode = subprocess.call([prog] + args, stdout=sys.stdout, stderr=sys.stderr,
                                  env=prog_env, cwd=prog_cwd)
        return retcode

    def _test_building(self, args):
        """
        Run building of test script.

        :param args: additional CLI options for PyInstaller.

        Return True if build succeded False otherwise.
        """
        default_args = ['--debug', '--noupx',
                '--specpath', self._specdir,
                '--distpath', self._distdir,
                '--workpath', self._builddir]
        default_args.extend(['--debug', '--log-level=DEBUG'])

        # Choose bundle mode.
        if self._mode == 'onedir':
            default_args.append('--onedir')
        elif self._mode == 'onefile':
            default_args.append('--onefile')

        pyi_args = [self.script] + default_args + args
        # TODO fix return code in running PyInstaller programatically
        PYI_CONFIG = configure.get_config(upx_dir=None)
        # Override CONFIGDIR for PyInstaller and put it into self.tmpdir
        PYI_CONFIG['configdir'] = self._tmpdir
        # Speed up tests by reusing copy of basic module graph object.
        PYI_CONFIG['tests_modgraph'] = copy.deepcopy(self._modgraph)
        pyi_main.run(pyi_args, PYI_CONFIG)
        retcode = 0

        return retcode == 0

    def _test_logs(self):
        """
        Compare log files (now used only by multipackage test_name).

        Return True if .toc files match or when .toc patters
        are not defined.
        """
        logsfn = glob.glob(self.test_file + '.toc')
        # Other main scripts do not start with 'test_'.
        assert self.test_file.startswith('test_')
        logsfn += glob.glob(self.test_file[5:] + '_?.toc')
        # generate a mapping basename -> pathname
        progs = dict((os.path.splitext(os.path.basename(nm))[0], nm)
                     for nm in self._find_exepath(self.test_file))
        for logfn in logsfn:
            self._msg("EXECUTING MATCHING " + logfn)
            tmpname = os.path.splitext(logfn)[0]
            prog = progs.get(tmpname)
            if not prog:
                return False, 'Executable for %s missing' % logfn
            fname_list = archive_viewer.get_archive_content(prog)
            # the archive contains byte-data, need to decode them
            fname_list = [fn.decode('utf-8') for fn in fname_list]
            pattern_list = eval(open(logfn, 'rU').read())
            # Alphabetical order of patterns.
            pattern_list.sort()
            missing = []
            for pattern in pattern_list:
                for fname in fname_list:
                    if re.match(pattern, fname):
                        self._plain_msg('MATCH: %s --> %s' % (pattern, fname))
                        break
                else:
                    # no matching entry found
                    missing.append(pattern)
                    self._plain_msg('MISSING: %s' % pattern)

            # Not all modules matched.
            # Stop comparing other .toc files and fail the test.
            if missing:
                msg = '\n'.join('Missing %s in %s' % (m, prog)
                                for m in missing)
                return False, msg

        return True, ''


# Scope 'session' should keep the object unchanged for whole tests.
# This fixture caches basic module graph dependencies that are same
# for every executable.
@pytest.fixture(scope='session')
def pyi_modgraph():
    return initialize_modgraph()


# Run by default test as onedir and onefile.
@pytest.fixture(params=['onedir', 'onefile'])
def pyi_builder(tmpdir, monkeypatch, request, pyi_modgraph):
    tmp = tmpdir.strpath
    # Append _MMODULES_DIR to sys.path for building exes.
    # Some tests need additional test modules.
    # This also ensures that sys.path is reseted to original value for every test.
    monkeypatch.syspath_prepend(_MODULES_DIR)
    # Save/restore environment variable PATH.
    monkeypatch.setenv('PATH', os.environ['PATH'], )
    # Set current working directory to
    monkeypatch.chdir(tmp)

    return AppBuilder(tmp, request.param, pyi_modgraph)
