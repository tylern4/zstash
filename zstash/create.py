from __future__ import absolute_import, print_function

import argparse
import errno
import logging
import os.path
import sqlite3
import sys
from typing import List, Tuple

from .hpss import hpss_put
from .hpss_utils import add_files
from .settings import DEFAULT_CACHE, config, get_db_filename, logger
from .utils import exclude_files, run_command

con = None
cur = None


def create():

    # Parser
    parser = argparse.ArgumentParser(
        usage="zstash create [<args>] path", description="Create a new zstash archive"
    )
    parser.add_argument("path", type=str, help="root directory to archive")
    required = parser.add_argument_group("required named arguments")
    required.add_argument(
        "--hpss",
        type=str,
        help='path to storage on HPSS. Set to "none" for local archiving. Must be set to "none" if the machine does not have HPSS access.',
        required=True,
    )
    optional = parser.add_argument_group("optional named arguments")
    optional.add_argument(
        "--exclude", type=str, help="comma separated list of file patterns to exclude"
    )
    optional.add_argument(
        "--maxsize",
        type=float,
        help="maximum size of tar archives (in GB, default 256)",
        default=256,
    )
    optional.add_argument(
        "--keep",
        help='if --hpss is not "none", keep the tar files in the local archive (cache) after uploading to the HPSS archive. Default is to delete the tar files. If --hpss=none, this flag has no effect.',
        action="store_true",
    )
    optional.add_argument(
        "--cache",
        type=str,
        help='the path to the zstash archive on the local file system. The default name is "zstash".',
    )
    optional.add_argument(
        "-v", "--verbose", action="store_true", help="increase output verbosity"
    )
    # Now that we're inside a subcommand, ignore the first two argvs
    # (zstash create)
    args = parser.parse_args(sys.argv[2:])
    if args.hpss and args.hpss.lower() == "none":
        args.hpss = "none"
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Copy configuration
    config.path = os.path.abspath(args.path)
    config.hpss = args.hpss
    # FIXME: Incompatible types in assignment (expression has type "int", variable has type "None") mypy(error)
    # Solution: https://stackoverflow.com/a/42279784
    config.maxsize = int(1024 * 1024 * 1024 * args.maxsize)  # type: ignore
    config.keep = args.keep
    if args.cache:
        cache = args.cache
    else:
        cache = DEFAULT_CACHE

    # Start doing actual work
    logger.debug("Running zstash create")
    logger.debug("Local path : %s" % (config.path))
    logger.debug("HPSS path  : %s" % (config.hpss))
    logger.debug("Max size  : %i" % (config.maxsize))  # type: ignore
    logger.debug("Keep local tar files  : %s" % (config.keep))

    # Make sure input path exists and is a directory
    logger.debug("Making sure input path exists and is a directory")
    # FIXME: Argument 1 to "isdir" has incompatible type "None"; expected "Union[str, bytes, _PathLike[str], _PathLike[bytes]]"mypy(error)
    if not os.path.isdir(config.path):  # type: ignore
        error_str = "Input path should be a directory: {}".format(config.path)
        logger.error(error_str)
        raise Exception(error_str)

    if config.hpss != "none":
        # Create target HPSS directory if needed
        logger.debug("Creating target HPSS directory")
        command = "hsi -q mkdir -p {}".format(config.hpss)
        error_str = "Could not create HPSS directory: {}".format(config.hpss)
        run_command(command, error_str)

        # Make sure it is empty
        logger.debug("Making sure target HPSS directory exists and is empty")

        command = 'hsi -q "cd {}; ls -l"'.format(config.hpss)
        error_str = "Target HPSS directory is not empty"
        run_command(command, error_str)

    # Create cache directory
    logger.debug("Creating local cache directory")
    # FIXME: Argument 1 to "chdir" has incompatible type "None"; expected "Union[int, Union[str, bytes, _PathLike[str], _PathLike[bytes]]]" mypy(error)
    os.chdir(config.path)  # type: ignore
    try:
        os.makedirs(cache)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            error_str = "Cannot create local cache directory"
            logger.error(error_str)
            raise Exception(error_str)
        pass

    # Verify that cache is empty
    # ...to do (?)

    # Create new database
    logger.debug("Creating index database")
    if os.path.exists(get_db_filename(cache)):
        os.remove(get_db_filename(cache))
    global con, cur
    con = sqlite3.connect(get_db_filename(cache), detect_types=sqlite3.PARSE_DECLTYPES)
    cur = con.cursor()

    # Create 'config' table
    cur.execute(
        u"""
create table config (
  arg text primary key,
  value text
);
    """
    )
    con.commit()

    # Create 'files' table
    cur.execute(
        u"""
create table files (
  id integer primary key,
  name text,
  size integer,
  mtime timestamp,
  md5 text,
  tar text,
  offset integer
);
    """
    )
    con.commit()

    # Store configuration in database
    for attr in dir(config):
        value = getattr(config, attr)
        if not callable(value) and not attr.startswith("__"):
            cur.execute(u"insert into config values (?,?)", (attr, value))
    con.commit()

    # List of files
    logger.info("Gathering list of files to archive")
    files: List[Tuple[str, str]] = []
    for root, dirnames, filenames in os.walk("."):
        # Empty directory
        if not dirnames and not filenames:
            files.append((root, ""))
        # Loop over files
        for filename in filenames:
            files.append((root, filename))

    # Sort files by directories and filenames
    files = sorted(files, key=lambda x: (x[0], x[1]))

    # Relative file path, eliminating top level zstash directory
    # FIXME: Name 'files' already defined mypy(error)
    files: List[str] = [  # type: ignore
        os.path.normpath(os.path.join(x[0], x[1]))
        for x in files
        if x[0] != os.path.join(".", cache)
    ]

    # Eliminate files based on exclude pattern
    if args.exclude is not None:
        files = exclude_files(args.exclude, files)

    # Add files to archive
    failures = add_files(cur, con, -1, files, cache)

    # Close database and transfer to HPSS. Always keep local copy
    con.commit()
    con.close()
    hpss_put(config.hpss, get_db_filename(cache), cache, keep=True)

    # List failures
    if len(failures) > 0:
        logger.warning("Some files could not be archived")
        for file_path in failures:
            logger.error("Archiving %s" % (file_path))