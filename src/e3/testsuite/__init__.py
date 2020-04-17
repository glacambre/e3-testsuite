"""Generic testsuite framework."""

from e3.collection.dag import DAG
from e3.env import Env, BaseEnv
from e3.fs import rm, mkdir, mv
from e3.job import Job
from e3.job.scheduler import Scheduler
from e3.main import Main
from e3.os.process import quote_arg
from e3.testsuite.report.xunit import dump_xunit_report
from e3.testsuite.result import TestResult, TestStatus
from e3.testsuite.testcase_finder import ProbingError, YAMLTestFinder

import traceback
import logging
import os
import yaml
import sys
import re
import tempfile

from colorama import Fore, Style

logger = logging.getLogger("testsuite")


class TooManyErrors(Exception):
    pass


class TestAbort(Exception):
    pass


def isatty(stream):
    """Return whether stream is a TTY.

    This is a safe predicate: it works if stream is None or if it does not even
    support TTY detection: in these cases, be conservative (consider it's not a
    TTY).
    """
    return stream and getattr(stream, 'isatty') and stream.isatty()


class DummyColors(object):
    """Stub to replace colorama's Fore/Style when colors are disabled."""

    def __getattr__(self, name):
        return ''


class TestFragment(Job):
    """Job used in a testsuite.

    :ivar test_instance: a TestDriver instance
    :ivar data: a function to call with the following signature (,) -> None
    """

    def __init__(self, uid, test_instance, fun, previous_values, notify_end):
        """Initialize a TestFragment.

        :param uid: uid of the test fragment (should be unique)
        :type uid: str
        :param test_instance: a TestDriver instance
        :type test_instance: e3.testsuite.driver.TestDriver
        :param fun: callable to be executed by the job
        :type fun: (,) -> None
        :param notify_end: Internal parameter. See e3.job
        :type notify_end: str -> None
        """
        super(TestFragment, self).__init__(uid, fun, notify_end)
        self.test_instance = test_instance
        self.previous_values = previous_values

    def run(self):
        """Run the test fragment."""
        self.return_value = None
        try:
            self.return_value = self.data(self.previous_values)
        except TestAbort:
            pass
        except Exception as e:
            # In case of exception generate a test result. The name is based
            # on the test name with an additional random part to avoid
            # conflicts
            logger.exception("got exception in test: %s", e)
            test = self.test_instance
            test.push_result(
                TestResult(
                    "%s__except%s" % (test.test_name, self.index),
                    env=test.test_env,
                    status=TestStatus.ERROR,
                )
            )
            self.return_value = e


class TestsuiteCore(object):
    """Testsuite Core driver.

    This class is the base of Testsuite class and should not be instanciated.
    It's not recommended to override any of the functions declared in it.

    See documentation of Testsuite class for overridable methods and
    variables.
    """

    def __init__(self, root_dir, testsuite_name="Untitled testsute"):
        """Testsuite constructor.

        :param root_dir: root dir of the testsuite. Usually the directory in
            which testsuite.py and runtest.py are located
        :param str testsuite_name: Name for this testsuite. It can be used to
            provide a title in some report formats.
        :type root_dir: str | unicode
        """
        self.root_dir = os.path.abspath(root_dir)
        self.test_dir = os.path.join(self.root_dir, self.TEST_SUBDIR)
        self.consecutive_failures = 0
        self.return_values = {}
        self.results = {}
        self.result_tracebacks = {}
        self.test_counter = 0
        self.test_status_counters = {s: 0 for s in TestStatus}
        self.testsuite_name = testsuite_name

    def test_result_filename(self, test_name):
        """Return the name of the file in which the result are stored.

        :param test_case_file: path to a test case scenario relative to the
            test directory
        :type test_case_file: str | unicode
        :param variant: the test variant
        :type variant: str
        :return: the test name. Note that test names should not contain path
            separators
        :rtype: str | unicode
        """
        return os.path.join(self.output_dir, test_name + ".yaml")

    def job_factory(self, uid, data, predecessors, notify_end):
        """Run internal function.

        See e3.job.scheduler
        """
        # we assume that data[0] is the test instance and data[1] the method
        # to call

        # When passing return values from predecessors, remove current test
        # name from the keys to ease referencing by user (the short fragment
        # name can then be used by user without knowing the full node id).
        key_prefix = data[0].test_name + "."
        key_prefix_len = len(key_prefix)

        def filter_key(k):
            if k.startswith(key_prefix):
                return k[key_prefix_len:]
            else:
                return k

        return TestFragment(
            uid,
            data[0],
            data[1],
            {filter_key(k): self.return_values[k] for k in predecessors},
            notify_end,
        )

    def testsuite_main(self, args=None):
        """Main for the main testsuite script.

        :param args: command line arguments. If None use sys.argv
        :type args: list[str] | None
        """
        self.main = Main(platform_args=self.CROSS_SUPPORT)

        # Add common options
        parser = self.main.argument_parser
        parser.add_argument(
            "-o",
            "--output-dir",
            metavar="DIR",
            default="./out",
            help="select output dir",
        )
        parser.add_argument("-t", "--temp-dir", metavar="DIR",
                            default=Env().tmp_dir)
        parser.add_argument(
            "-d", "--dev-temp",
            nargs="?", default=None, const="tmp",
            help="Unlike --temp-dir, use this very directory to store"
                 " testsuite temporaries (i.e. no random subdirectory). Also"
                 " automatically disable temp dir cleanup, to be developer"
                 " friendly. If no directory is provided, use the local"
                 " \"tmp\" directory")
        parser.add_argument(
            "--max-consecutive-failures",
            default=0,
            help="If there are more than N consecutive failures, the testsuite"
            " is aborted. If set to 0 (default) then the testsuite will never"
            " be stopped",
        )
        parser.add_argument(
            "--keep-old-output-dir",
            default=False,
            action="store_true",
            help="This is default with this testsuite framework. The option"
            " is kept only to keep backward compatibility of invocation with"
            " former framework (gnatpython.testdriver)",
        )
        parser.add_argument(
            "--disable-cleanup",
            dest="enable_cleanup",
            action="store_false",
            default=True,
            help="disable cleanup of working space",
        )
        parser.add_argument(
            "-j",
            "--jobs",
            dest="jobs",
            type=int,
            metavar="N",
            default=Env().build.cpu.cores,
            help="Specify the number of jobs to run simultaneously",
        )
        parser.add_argument(
            "--show-error-output",
            "-E",
            action="store_true",
            help="When testcases fail, display their output. This is for"
            " convenience for interactive use.",
        )
        parser.add_argument(
            "--dump-environ",
            dest="dump_environ",
            action="store_true",
            default=False,
            help="Dump all environment variables in a file named environ.sh,"
            " located in the output directory (see --output-dir). This"
            " file can then be sourced from a Bourne shell to recreate"
            " the environement that existed when this testsuite was run"
            " to produce a given testsuite report.",
        )
        parser.add_argument(
            "--xunit-output",
            dest="xunit_output",
            metavar="FILE",
            help="Output testsuite report to the given file in the standard"
            " XUnit XML format. This is useful to display results in"
            " continuous build systems such as Jenkins.",
        )
        parser.add_argument(
            "sublist", metavar="tests", nargs="*", default=[], help="test"
        )
        # Add user defined options
        self.add_options()

        # parse options
        self.main.parse_args(args)

        # If there is a chance for the logging to end up in a non-tty stream,
        # disable colors.
        self.Fore = Fore
        self.Style = Style
        enable_colors = True
        if (
            self.main.args.log_file
            or not isatty(sys.stdout)
            or not isatty(sys.stderr)
        ):
            enable_colors = False
            self.Fore = DummyColors()
            self.Style = DummyColors()

        self.env = BaseEnv.from_env()
        self.env.enable_colors = enable_colors
        self.env.root_dir = self.root_dir
        self.env.test_dir = self.test_dir

        # At this stage compute commonly used paths
        # Keep the working dir as short as possible, to avoid the risk
        # of having a path that's too long (a problem often seen on
        # Windows, or when using WRS tools that have their own max path
        # limitations).
        # Note that we do make sure that working_dir is an absolute
        # path, as we are likely to be changing directories when
        # running each test. A relative path would no longer work
        # under those circumstances.
        d = os.path.abspath(self.main.args.output_dir)
        self.output_dir = os.path.join(d, "new")
        self.old_output_dir = os.path.join(d, "old")

        if self.main.args.dev_temp:
            # Use a temporary directory for developers: make sure it is an
            # empty directory and disable cleanup to ease post-mortem
            # investigation.
            self.working_dir = os.path.abspath(self.main.args.dev_temp)
            rm(self.working_dir, recursive=True)
            mkdir(self.working_dir)
            self.main.args.enable_cleanup = False

        else:
            # If the temp dir is supposed to be randomized, we need to create a
            # subdirectory, so check that the parent directory exists first.
            if not os.path.isdir(self.main.args.temp_dir):
                logger.critical("temp dir '%s' does not exist",
                                self.main.args.temp_dir)
                return 1

            self.working_dir = tempfile.mkdtemp(
                "", "tmp", os.path.abspath(self.main.args.temp_dir))

        # Create the new output directory that will hold the results
        self.setup_result_dir()

        # Store in global env: target information and common paths
        self.env.output_dir = self.output_dir
        self.env.working_dir = self.working_dir
        self.env.options = self.main.args

        # User specific startup
        self.set_up()

        # Retrieve the list of test
        self.has_error = False
        self.test_list = self.get_test_list(self.main.args.sublist)

        # Launch the mainloop
        self.total_test = len(self.test_list)
        self.run_test = 0

        self.scheduler = Scheduler(
            job_provider=self.job_factory,
            collect=self.collect_result,
            tokens=self.main.args.jobs,
        )
        actions = DAG()
        for parsed_test in self.test_list:
            if not self.add_test(actions, parsed_test):
                self.has_error = True

        with open(os.path.join(self.output_dir, "tests.dot"), "w") as fd:
            fd.write(actions.as_dot())
        self.scheduler.run(actions)

        self.dump_testsuite_result()
        if self.main.args.xunit_output:
            dump_xunit_report(self, self.main.args.xunit_output)

        # Clean everything
        self.tear_down()
        return 1 if self.has_error else 0

    def get_test_list(self, sublist):
        """Retrieve the list of tests.

        :param list[str] sublist: A list of tests scenarios or patterns.
        :rtype: list[str]
        """
        # Use a mapping: absolute test directory -> ParsedTest when building
        # the result, as several patterns in "sublist" may yield the same
        # testcase.
        result = {}
        test_finders = self.test_finders

        def helper(pattern):
            # If the given pattern is a directory, do not go through the whole
            # tests subdirectory.
            if os.path.isdir(pattern):
                root = pattern
                pattern = None
            else:
                root = self.test_dir
                try:
                    pattern = re.compile(pattern)
                except re.error as exc:
                    self.has_error = True
                    logger.error(
                        "Invalid test pattern, skipping: {} ({})".format(
                            pattern, exc
                        )
                    )
                    return

            # For each directory in the requested subdir, ask our test finders
            # to probe for a testcase. Register matches.
            for dirpath, dirnames, filenames in os.walk(root):
                # If the directory name does not match the given pattern, skip
                # it.
                if pattern is not None and not pattern.search(dirpath):
                    continue

                # The first test finder that has a match "wins"
                for tf in test_finders:
                    try:
                        test = tf.probe(self, dirpath, dirnames, filenames)
                    except ProbingError as exc:
                        self.has_error = True
                        logger.error(str(exc))
                        break
                    if test is not None:
                        result[test.test_dir] = test
                        break

        # If specific tests are requested, only look for them. Otherwise, just
        # look in the tests subdirectory.
        if sublist:
            for s in sublist:
                helper(s)
        else:
            helper(self.test_dir)

        result = list(result.values())
        logger.info("Found {} tests".format(len(result)))
        logger.debug("tests:\n  " + "\n  ".join(t.test_dir for t in result))
        return result

    def add_test(self, actions, parsed_test):
        """Register a test to run.

        :param e3.collection.dag.DAG actions: The dag of actions for the
            testsuite.
        :param e3.testsuite.testcase_finder.ParsedTest test_case_file: Test to
            instantiate.

        :return: Whether the test was successfully registered.
        :rtype: bool
        """
        test_name = parsed_test.test_name

        # Complete the test environment
        test_env = dict(parsed_test.test_env)
        test_env["test_dir"] = parsed_test.test_dir
        test_env["test_name"] = test_name
        test_env["working_dir"] = os.path.join(self.env.working_dir, test_name)

        # Fetch the test driver to use
        driver = parsed_test.driver_cls
        if not driver:
            if self.default_driver:
                driver = self.DRIVERS[self.default_driver]
            else:
                logger.error("missing driver for test '{}'".format(test_name))
                return False

        # Finally run the driver instantiation
        try:
            instance = driver(self.env, test_env)
            instance.Fore = self.Fore
            instance.Style = self.Style
            instance.add_test(actions)

        except Exception as e:
            error_msg = str(e)
            error_msg += "\nTraceback:\n"
            error_msg += "\n".join(traceback.format_tb(sys.exc_info()[2]))
            logger.error(error_msg)
            return False

        return True

    def dump_testsuite_result(self):
        """Log a summary of test results.

        Subclasses are free to override this to do whatever is suitable for
        them.
        """
        lines = ['Summary:']

        # Display test count for each status, but only for status that have
        # at least one test. Sort them by status value, to get consistent
        # order.
        def sort_key(couple):
            status, _ = couple
            return status.value
        stats = sorted(((status, count)
                        for status, count in self.test_status_counters.items()
                        if count),
                       key=sort_key)
        for status, count in stats:
            lines.append('  {}{: <12}{} {}'.format(
                status.color(self), status.name, self.Style.RESET_ALL, count))
        if not stats:
            lines.append('  <no test result>')
        logger.info('\n'.join(lines))

        # Dump the comment file
        with open(os.path.join(self.output_dir, "comment"), "w") as f:
            self.write_comment_file(f)

    def collect_result(self, job):
        """Run internal function.

        :param job: a job that is finished
        :type job: TestFragment
        """
        self.return_values[job.uid] = job.return_value
        while job.test_instance.result_queue:
            result, tb = job.test_instance.result_queue.pop()

            # Log the test result. If requested, include test log
            log_line = '{}{: <12}{} {}{}{}'.format(
                result.status.color(self),
                result.status.name,
                self.Style.RESET_ALL,

                self.Style.BRIGHT,
                result.test_name,
                self.Style.RESET_ALL)
            if result.msg:
                log_line += ': {}{}{}'.format(self.Style.DIM, result.msg,
                                              self.Style.RESET_ALL)
            if (
                self.main.args.show_error_output
                and result.status not in (TestStatus.PASS, TestStatus.XFAIL,
                                          TestStatus.XPASS)
            ):
                log_line += '\n' + str(result.log) + self.Style.RESET_ALL
            logger.info(log_line)

            def indented_tb(tb):
                return "".join("  {}".format(line) for line in tb)

            assert result.test_name not in self.results, (
                "cannot push twice results for {}"
                "\nFirst push happened at:"
                "\n{}"
                "\nThis one happened at:"
                "\n{}".format(
                    result.test_name,
                    indented_tb(self.result_tracebacks[result.test_name]),
                    indented_tb(tb),
                )
            )
            with open(self.test_result_filename(result.test_name), "w") as fd:
                yaml.dump(result, fd)
            self.results[result.test_name] = result.status
            self.result_tracebacks[result.test_name] = tb
            self.test_counter += 1
            self.test_status_counters[result.status] += 1
        return False

    def setup_result_dir(self):
        """Create the output directory in which the results are stored."""
        if os.path.isdir(self.old_output_dir):
            rm(self.old_output_dir, True)
        if os.path.isdir(self.output_dir):
            mv(self.output_dir, self.old_output_dir)
        mkdir(self.output_dir)

        if self.main.args.dump_environ:
            with open(os.path.join(self.output_dir, "environ.sh"), "w") as f:
                for var_name in sorted(os.environ):
                    f.write("export {}={}\n".format(
                        var_name, quote_arg(os.environ[var_name])))


class Testsuite(TestsuiteCore):
    """Testsuite class.

    When implementing a new testsuite you should create a class that
    inherit from this class.
    """

    CROSS_SUPPORT = False
    """
    Whether this testsuite supports --target/--build/--host cmdline arguments.
    """

    TEST_SUBDIR = "."
    """
    Name of the directory that contains testcases.

    This name is relative to the testsuite root directory, as passed to the
    Testsuite constructor.
    """

    DRIVERS = {}
    """
    Map from test driver names to TestDriver subclasses.
    """

    @property
    def default_driver(self):
        """Return the default driver for testcases.

        The return value is used only if the test.yaml file does not contain
        any ``driver`` key. Note that you have access to the current test.yaml
        location using the attribute ``self.test_case_file``.

        If the result is None, consider that "driver" is a mandatory key in
        test.yaml files.

        :return: the driver to be used by default
        :rtype: str|None
        """
        return None

    def test_name(self, test_dir):
        """Compute the test name given a testcase spec.

        This function can be overridden. By default it uses the name of the
        test directory. Note that the test name should be a valid filename (not
        dir seprators, or special characters such as ``:``, ...).

        :param str test_dir: Directory that contains the testcase.
        :rtype: str
        """
        # Start with a relative directory name from the tests subdirectory
        result = os.path.relpath(test_dir, self.test_dir)

        # We want to support running tests outside of the test directory, so
        # strip leading "..".
        pattern = os.path.pardir + os.path.sep
        while result.startswith(pattern):
            result = result[len(pattern):]

        # Run some name canonicalization and replace directory separators with
        # double underscores.
        return result.replace("\\", "/").rstrip("/").replace("/", "__")

    @property
    def test_finders(self):
        """Return test finders to probe tests directories.

        :rtype: list[e3.testsuite.testcase_finder.TestFinder]
        """
        return [YAMLTestFinder()]

    def add_options(self):
        """Add testsuite specific switches.

        We can add your own switches by calling self.main.add_option
        function
        """
        pass

    def set_up(self):
        """Execute operations before running the testsuite.

        Before running this, command-line arguments were parsed. After this
        returns, the testsuite will look for testcases.

        By default, this does nothing. Overriding this method allows testsuites
        to prepare the execution of the testsuite depending on their needs. For
        instance:

        * process testsuite-specific options;
        * initialize environment variables;
        * adjust self.env (object forwarded to test drivers).
        """
        return self.tear_up()

    def tear_up(self):
        """For backwards compatibility, alternative for the "set_up" method."""
        pass

    def tear_down(self):
        """Execute operation when finalizing the testsuite.

        By default, this cleans the working (temporary) directory in which the
        tests were run.
        """
        if self.main.args.enable_cleanup:
            rm(self.working_dir, True)

    def write_comment_file(self, comment_file):
        """Write the comment file's content.

        :param file comment_file: File descriptor for the comment file.
            Overriding methods should only call its "write" method (or print to
            it).
        """
        pass
